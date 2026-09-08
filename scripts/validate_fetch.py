#!/usr/bin/env python3
"""3단계 검증: src/fetch.py 를 실제 KRX 에 대해 실행 — 호출별 소요시간, 캐시 히트, 관리종목 소스(KIND/소속부) 결과.

- docs/results/universe_result.json (2단계) / stage1_result.json (1단계) 이 있으면 listed·ETF·ETN 개수를 자동 대조
- 출력: stdout + docs/results/fetch_result.json (+ fetch_result_<T>.json)

사용:
    export KRX_ID=...; export KRX_PW=...
    python scripts/validate_fetch.py [--base-date YYYYMMDD] [--cache-dir data/cache]
종료코드: 0 성공 / 1 일부 실패 / 2 자격증명 없음
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

from src.calendar import today_kst, all_windows, resolve_base_date  # noqa: E402
from src.fetch import Fetcher, KRXCredentialsError, KRXUnavailableError  # noqa: E402
from src.universe import administrative_from_sect, build_universe  # noqa: E402

RESULTS_DIR = ROOT / "docs" / "results"
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
calls: list[dict] = []


def timed(label, fn, *args, **kwargs):
    t0 = time.perf_counter()
    try:
        out = fn(*args, **kwargs)
        n = len(out) if hasattr(out, "__len__") else None
        calls.append({"call": label, "ok": True, "sec": round(time.perf_counter() - t0, 2), "rows": n})
        print(f"[OK ] {label:<50} {time.perf_counter() - t0:6.2f}s rows={n}")
        return out
    except (KRXUnavailableError, Exception) as e:  # noqa: BLE001
        calls.append({"call": label, "ok": False, "sec": round(time.perf_counter() - t0, 2), "rows": None,
                      "error": f"{type(e).__name__}: {str(e)[:200]}"})
        print(f"[ERR] {label:<50} {type(e).__name__}: {str(e)[:200]}")
        return None


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-date", default=today_kst())
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    import yaml
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    fetcher = Fetcher(config, cache_dir=args.cache_dir)
    total_t0 = time.perf_counter()
    summary: dict = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "markets": {}}

    try:
        T = resolve_base_date(args.base_date, fetcher.nearest)
    except KRXCredentialsError as e:
        print(str(e), file=sys.stderr)
        return 2
    except KRXUnavailableError as e:
        summary["error"] = str(e)
        summary["failure"] = {"classification": e.classification, **e.diagnosis}
        print(f"KRX 접근 불가 [{e.classification}]: {e}", file=sys.stderr)
        return _finish(summary, fetcher, total_t0, T=None)
    if T is None:
        T = fetcher.nearest(args.base_date, True)
        summary["note"] = f"{args.base_date} 는 휴장일 — 검증은 T={T} 로 진행"
    summary["T"] = T
    windows = all_windows(T, config["periods"], fetcher.nearest)
    summary["windows"] = {p: {"from": w.from_date, "base_date": w.base_date} for p, w in windows.items()}
    w1 = windows["1w"]

    etf = timed("etf_tickers(T)", fetcher.etf_tickers, T)
    etn = timed("etn_tickers(T)", fetcher.etn_tickers, T)
    summary["n_etf"], summary["n_etn"] = len(etf or []), len(etn or [])

    kind_codes = None
    for market in config["markets"]:
        m: dict = {}
        listed = timed(f"listed(T, {market})", fetcher.listed, T, market)
        if listed is None:
            summary["markets"][market] = {"error": "listed 실패"}
            continue
        m["n_listed"] = int(len(listed))
        idx = timed(f"index_ohlcv({w1.base_date}, T, {market})", fetcher.index_ohlcv, w1.base_date, T, market)
        if idx is not None and len(idx):
            m["index_1w"] = {"base_close": float(idx["종가"].iloc[0]), "close_T": float(idx["종가"].iloc[-1]),
                             "first": str(idx.index[0].date()), "last": str(idx.index[-1].date())}
        timed(f"market_cap(T, {market})", fetcher.market_cap, T, market)
        pc = timed(f"price_change({w1.from_date}, T, {market})", fetcher.price_change, w1.from_date, T, market)
        if pc is not None:
            m["n_price_change_1w"] = int(len(pc))
            m["n_delisted_rows"] = int(((pc["종가"] == 0) & (pc["등락률"] == -100)).sum())

        # 관리종목: KIND → 소속부 → None
        admin = timed(f"administrative_tickers(T, {market})", fetcher.administrative_tickers, T, market, listed)
        sect_set = administrative_from_sect(listed) or set()
        if kind_codes is None:
            kind_codes = fetcher._kind_administrative_codes(T)  # 캐시됨
        m["administrative"] = {
            "source": ("KIND" if kind_codes else ("sect" if market == "KOSDAQ" else "none")),
            "n": (len(admin) if admin is not None else None),
            "kind_codes_total": (len(kind_codes) if kind_codes else 0),
            "kind_in_listed": (len(kind_codes & set(listed.index)) if kind_codes else 0),
            "sect_n": len(sect_set),
            "kind_vs_sect_overlap": (len((kind_codes or set()) & sect_set)),
            "sample": sorted(admin)[:10] if admin else [],
        }
        res = build_universe(listed, market, config, etf_tickers=etf or [], etn_tickers=etn, administrative_tickers=admin)
        m["steps"], m["warnings"] = res.steps, res.warnings
        print(res.summary())
        summary["markets"][market] = m

    summary["kind_debug"] = fetcher.kind_debug

    # 캐시 히트 확인: 같은 호출을 다시 하면 네트워크 없이 즉시 반환
    before = dict(fetcher.stats)
    t0 = time.perf_counter()
    fetcher.listed(T, config["markets"][0]); fetcher.etf_tickers(T)
    summary["cache_check"] = {"sec_for_2_cached_calls": round(time.perf_counter() - t0, 3),
                              "cache_hits_delta": fetcher.stats["cache_hits"] - before["cache_hits"],
                              "network_calls_delta": fetcher.stats["network_calls"] - before["network_calls"]}

    # 이전 검증 결과와 자동 대조
    prev_u = load_json(RESULTS_DIR / "universe_result.json")
    prev_s = load_json(RESULTS_DIR / "stage1_result.json")
    cmp: dict = {}
    if prev_u:
        cmp["universe"] = {"T": prev_u.get("T"), "same_T": prev_u.get("T") == T,
                           "n_etf": (prev_u.get("n_etf"), summary["n_etf"]), "n_etn": (prev_u.get("n_etn"), summary["n_etn"])}
        for mk, pm in (prev_u.get("markets") or {}).items():
            cmp["universe"][f"n_listed_{mk}"] = (pm.get("n_listed"), summary["markets"].get(mk, {}).get("n_listed"))
    if prev_s:
        cmp["stage1"] = {"T": prev_s.get("T"), "n_kospi": (prev_s.get("n_kospi"), summary["markets"].get("KOSPI", {}).get("n_listed")),
                         "n_kosdaq": (prev_s.get("n_kosdaq"), summary["markets"].get("KOSDAQ", {}).get("n_listed")),
                         "n_etf": (prev_s.get("n_etf"), summary["n_etf"])}
    summary["compare_previous"] = cmp or None
    return _finish(summary, fetcher, total_t0, T=T)


def _finish(summary: dict, fetcher: Fetcher, total_t0: float, T: str | None) -> int:
    summary["total_sec"] = round(time.perf_counter() - total_t0, 1)
    summary["fetch_stats"] = fetcher.stats
    summary["calls"] = calls
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    (RESULTS_DIR / "fetch_result.json").write_text(text, encoding="utf-8")
    print("\n=== 요약 ===")
    for mk, m in summary.get("markets", {}).items():
        if "steps" in m:
            print(f"[{mk}] " + " → ".join(f"{k}={v}" for k, v in m["steps"]) + f" | 관리종목: {m['administrative']}")
    print(f"cache: {summary.get('cache_check')} | stats: {fetcher.stats}")
    print(f"compare: {summary.get('compare_previous')}")
    print(f"total {summary['total_sec']}s → {RESULTS_DIR / 'fetch_result.json'}")
    return 0 if all(c["ok"] for c in calls) and T else 1


if __name__ == "__main__":
    sys.exit(main())
