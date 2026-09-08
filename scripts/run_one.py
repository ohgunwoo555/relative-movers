#!/usr/bin/env python3
"""4단계 검증: 한 (시장, 기간) 조합만 실행 — src/main.py 의 얇은 래퍼.

    export KRX_ID=...; export KRX_PW=...
    python scripts/run_one.py --market KOSPI --period 1d [--base-date YYYYMMDD] [--top-n 20]

출력: stdout 마크다운 표 + docs/results/calc_result.json (+ calc_result_<T>_<market>_<period>.json)
      + outputs/<T>/movers_<market>_<period>.csv (gitignore)
종료코드: 0 성공 / 1 실패(KRX 접근 불가 — 결과 JSON `failure.classification`) / 2 자격증명 없음 / 3 휴장일(T 없음)
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

from src.fetch import Fetcher, KRXCredentialsError  # noqa: E402
from src.main import EXIT_CREDENTIALS, load_config, run  # noqa: E402
from src.rank import to_markdown_table  # noqa: E402
from src.report import write_csv  # noqa: E402

RESULTS_DIR = ROOT / "docs" / "results"
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KOSPI", choices=["KOSPI", "KOSDAQ"])
    ap.add_argument("--period", default="1d")
    ap.add_argument("--base-date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    config = load_config(args.config)
    if args.period not in config["periods"]:
        print(f"period must be one of {list(config['periods'])}", file=sys.stderr)
        return 1
    fetcher = Fetcher(config, cache_dir=args.cache_dir)
    t0 = time.perf_counter()
    summary: dict = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "market": args.market, "period": args.period}
    try:
        res = run(config, fetcher, args.base_date, markets=[args.market], periods=[args.period], top_n=args.top_n)
    except KRXCredentialsError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CREDENTIALS

    summary.update(T=res.T, top_n=res.top_n, note=res.note, fetch_stats=res.fetch_stats,
                   steps=[{"step": k, "sec": v} for k, v in res.timings.items()])
    if res.failure:
        summary["failure"] = res.failure
        summary["error"] = res.failure.get("error")
    elif res.T is None:
        print(res.note)
    else:
        c = res.combos[0]
        summary["window"] = c.window
        summary["universe"] = res.universes.get(args.market, {})
        summary["steps"] += [{"step": f"combo/{k}", "sec": v} for k, v in c.timings.items()]
        if c.ok:
            summary["inputs"] = c.inputs
            summary["calc"] = {"n_rows": c.n_calc_rows, "market_ret": c.market_ret, "excess_ret_stats": c.excess_stats}
            summary["rankings"] = json.loads(c.ranked.to_json(orient="records", force_ascii=False))
            summary["tables"] = {"up": to_markdown_table(c.ranked, "up"), "down": to_markdown_table(c.ranked, "down")}
            out_dir = ROOT / config.get("output", {}).get("dir", "outputs") / res.T
            csv_path = write_csv(c.ranked, out_dir / f"movers_{args.market}_{args.period}.csv")
            summary["csv"] = str(csv_path.relative_to(ROOT))
            print(f"\n## {args.market} {args.period} — T={res.T}, from={c.window['from']}, 기준가일={c.window['base_date']}, "
                  f"market_ret={c.market_ret:+.3f}%, universe={res.universes[args.market]['steps'][-1][1]}, calc_rows={c.n_calc_rows}")
            for w in res.warnings:
                print(f"⚠️ {w}")
            print(f"\n### up {res.top_n}\n{summary['tables']['up']}\n\n### down {res.top_n}\n{summary['tables']['down']}")
        else:
            summary["error"], summary["error_class"] = c.error, c.error_class
            print(f"❌ {args.market} {args.period}: [{c.error_class}] {c.error}", file=sys.stderr)

    summary["total_sec"] = round(time.perf_counter() - t0, 1)
    summary["exit_code"] = res.exit_code
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    (RESULTS_DIR / "calc_result.json").write_text(text, encoding="utf-8")
    if res.T:
        (RESULTS_DIR / f"calc_result_{res.T}_{args.market}_{args.period}.json").write_text(text, encoding="utf-8")
    print(f"\ntotal {summary['total_sec']}s · fetch {res.fetch_stats} → {RESULTS_DIR / 'calc_result.json'}")
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
