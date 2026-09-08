#!/usr/bin/env python3
"""4단계 검증: 한 (시장, 기간) 조합을 calendar → fetch → universe → calc → rank 로 끝까지 실행한다.

    export KRX_ID=...; export KRX_PW=...
    python scripts/run_one.py --market KOSPI --period 1d [--base-date YYYYMMDD] [--top-n 20]

출력: stdout 마크다운 표 + docs/results/calc_result.json (+ calc_result_<T>_<market>_<period>.json)
      + outputs/<T>/movers_<market>_<period>.csv (gitignore)
종료코드: 0 성공 / 1 실패 / 2 자격증명 없음 / 3 휴장일(T 없음)

주: 2시장×5기간 루프와 통합 출력은 5단계 main.py 가 맡는다. 여기의 오케스트레이션은 그때 main.py 로 옮긴다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.calc import compute  # noqa: E402
from src.calendar import period_window, resolve_base_date  # noqa: E402
from src.fetch import Fetcher, KRXCredentialsError, KRXUnavailableError  # noqa: E402
from src.rank import to_markdown_table, top_and_bottom  # noqa: E402
from src.universe import build_universe  # noqa: E402

RESULTS_DIR = ROOT / "docs" / "results"
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run_one")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KOSPI", choices=["KOSPI", "KOSDAQ"])
    ap.add_argument("--period", default="1d")
    ap.add_argument("--base-date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    import yaml
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.period not in config["periods"]:
        print(f"period must be one of {list(config['periods'])}", file=sys.stderr)
        return 1
    top_n = args.top_n or int(config.get("top_n", 20))
    fetcher = Fetcher(config, cache_dir=args.cache_dir)
    t0 = time.perf_counter()
    summary: dict = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "market": args.market,
                     "period": args.period, "top_n": top_n, "steps": []}

    def step(name, fn, *a, **kw):
        s = time.perf_counter()
        out = fn(*a, **kw)
        summary["steps"].append({"step": name, "sec": round(time.perf_counter() - s, 2)})
        log.info("%-32s %6.2fs", name, time.perf_counter() - s)
        return out

    try:
        # 1. T 와 구간 (DESIGN.md 2절)
        T = step("resolve_base_date", resolve_base_date, args.base_date, fetcher.nearest)
        if T is None:
            summary["note"] = f"{args.base_date} 는 휴장일 — 종료"
            print(summary["note"])
            return _finish(summary, fetcher, t0, exit_code=3)
        w = step("period_window", period_window, args.period, T, config["periods"][args.period], fetcher.nearest)
        summary.update(T=T, window={"ref_date": w.ref_date, "from": w.from_date, "base_date": w.base_date})

        # 2. 유니버스 (DESIGN.md 3절)
        listed = step("fetch.listed", fetcher.listed, T, args.market)
        etf = step("fetch.etf_tickers", fetcher.etf_tickers, T)
        etn = step("fetch.etn_tickers", fetcher.etn_tickers, T)
        admin = step("fetch.administrative_tickers", fetcher.administrative_tickers, T, args.market, listed)
        uni = step("build_universe", build_universe, listed, args.market, config,
                   etf_tickers=etf, etn_tickers=etn, administrative_tickers=admin)
        summary["universe"] = {"steps": uni.steps, "warnings": uni.warnings}

        # 3. 시세 (DESIGN.md 4절)
        idx = step("fetch.index_ohlcv", fetcher.index_ohlcv, w.base_date, T, args.market)
        pc = step("fetch.price_change", fetcher.price_change, w.from_date, T, args.market)
        cap = step("fetch.market_cap", fetcher.market_cap, T, args.market)
        summary["inputs"] = {"n_price_change": int(len(pc)), "n_delisted_rows": int(((pc["종가"] == 0) & (pc["등락률"] == -100)).sum()),
                             "index_rows": int(len(idx)), "n_market_cap": int(len(cap))}

        # 4. 계산·랭킹 (DESIGN.md 2절·6절·7절)
        calc = step("calc.compute", compute, args.market, w, price_change=pc, index_df=idx,
                    universe=uni.df, market_cap=cap, config=config)
        ranked = step("rank.top_and_bottom", top_and_bottom, calc, top_n)
    except KRXCredentialsError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KRXUnavailableError as e:
        summary["error"] = str(e)
        print(f"KRX 접근 불가: {e}", file=sys.stderr)
        return _finish(summary, fetcher, t0, exit_code=1)

    summary["calc"] = {"n_rows": int(len(calc)), "market_ret": float(calc["market_ret"].iloc[0]) if len(calc) else None,
                       "excess_ret_stats": {k: round(float(v), 3) for k, v in calc["excess_ret"].describe().items()} if len(calc) else None}
    summary["rankings"] = json.loads(ranked.to_json(orient="records", force_ascii=False))
    summary["tables"] = {"up": to_markdown_table(ranked, "up"), "down": to_markdown_table(ranked, "down")}

    out_dir = ROOT / config.get("output", {}).get("dir", "outputs") / T
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"movers_{args.market}_{args.period}.csv"
    ranked.to_csv(csv_path, index=False, encoding="utf-8-sig")
    summary["csv"] = str(csv_path.relative_to(ROOT))

    print(f"\n## {args.market} {args.period} — T={T}, from={w.from_date}, 기준가일={w.base_date}, "
          f"market_ret={summary['calc']['market_ret']:+.3f}%, universe={len(uni.df)}, calc_rows={len(calc)}")
    for wmsg in uni.warnings:
        print(f"⚠️ {wmsg}")
    print(f"\n### up {top_n}\n{summary['tables']['up']}\n\n### down {top_n}\n{summary['tables']['down']}")
    return _finish(summary, fetcher, t0, exit_code=0)


def _finish(summary: dict, fetcher: Fetcher, t0: float, exit_code: int) -> int:
    summary["total_sec"] = round(time.perf_counter() - t0, 1)
    summary["fetch_stats"] = fetcher.stats
    summary["exit_code"] = exit_code
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    (RESULTS_DIR / "calc_result.json").write_text(text, encoding="utf-8")
    if summary.get("T"):
        (RESULTS_DIR / f"calc_result_{summary['T']}_{summary['market']}_{summary['period']}.json").write_text(text, encoding="utf-8")
    print(f"\ntotal {summary['total_sec']}s · fetch {fetcher.stats} → {RESULTS_DIR / 'calc_result.json'}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
