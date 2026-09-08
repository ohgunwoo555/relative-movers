#!/usr/bin/env python3
"""DESIGN.md 10절 1단계: 환경·데이터 검증 스크립트.

- pykrx 5종 함수(DESIGN.md 4절) 실제 호출 → 동작 여부·소요시간 측정
- 최근 1년 내 액면분할 종목으로 get_market_price_change 의 수정주가 반영 여부 검증
- 결과를 docs/stage1_result.json 과 stdout(markdown 표)으로 출력

사용:
    export KRX_ID=...; export KRX_PW=...      # pykrx>=1.2 필수 (KRX Data Marketplace 계정)
    python scripts/validate_stage1.py [--split-ticker 058430 --split-date 20260423 --market KOSPI]

pykrx 는 호출 간 sleep 을 넣지 않으므로 여기서 DESIGN.md 규칙(sleep 1s, 3회 재시도)을 그대로 적용한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

SLEEP_SEC = 1
MAX_RETRIES = 3
ROOT = Path(__file__).resolve().parent.parent
RESULT_JSON = ROOT / "docs" / "stage1_result.json"

results: list[dict] = []


def timed(label: str, fn, *args, **kwargs):
    """fn 을 재시도·sleep 규칙으로 호출하고 소요시간·행수·에러를 기록한다."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
            elapsed = time.perf_counter() - t0
            n = len(out) if hasattr(out, "__len__") else None
            results.append({"call": label, "ok": True, "sec": round(elapsed, 2),
                            "rows": n, "attempts": attempt})
            print(f"[OK ] {label:<55} {elapsed:6.2f}s rows={n}")
            time.sleep(SLEEP_SEC)
            return out
        except Exception as e:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[ERR] {label:<55} {elapsed:6.2f}s attempt={attempt} {last_err}")
            time.sleep(SLEEP_SEC * attempt)
    results.append({"call": label, "ok": False, "sec": None, "rows": None,
                    "attempts": MAX_RETRIES, "error": last_err})
    return None


def ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-date", default=ymd(dt.date.today()), help="실행일 YYYYMMDD (기본 오늘)")
    ap.add_argument("--split-ticker", default="058430", help="최근 1년 내 액면분할 종목 (기본 포스코스틸리온)")
    ap.add_argument("--split-date", default="20260423", help="분할 신주 변경상장일 YYYYMMDD")
    ap.add_argument("--market", default="KOSPI", help="분할 종목의 시장")
    ap.add_argument("--skip-1y", action="store_true", help="1년치 price_change 호출 생략(시간 절약)")
    args = ap.parse_args()

    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        print("KRX_ID / KRX_PW 환경변수가 없습니다. pykrx>=1.2 는 KRX Data Marketplace 로그인이 필요합니다.",
              file=sys.stderr)
        return 2

    total_t0 = time.perf_counter()
    summary: dict = {"run_at": dt.datetime.now().isoformat(timespec="seconds")}

    # pykrx>=1.2 는 import 시점에 KRX 로그인(warmup + login)을 수행하며, KRX 에 닿지 못하면
    # 예외를 그대로 던진다(auth.warmup_krx_session 에 try/except 없음). 여기서 잡아 기록한다.
    t0 = time.perf_counter()
    try:
        from pykrx import stock  # noqa: E402
        import pykrx
        results.append({"call": "import pykrx (KRX login at import)", "ok": True,
                        "sec": round(time.perf_counter() - t0, 2), "rows": None, "attempts": 1})
        summary["pykrx_version"] = getattr(pykrx, "__version__", "?")
    except Exception as e:  # noqa: BLE001
        results.append({"call": "import pykrx (KRX login at import)", "ok": False,
                        "sec": round(time.perf_counter() - t0, 2), "rows": None, "attempts": 1,
                        "error": f"{type(e).__name__}: {str(e)[:300]}"})
        print(f"[ERR] pykrx import/login 실패: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return _finish(summary, total_t0)

    # ── 1. 거래일 보정 ─────────────────────────────────────────────
    T = timed("get_nearest_business_day_in_a_week(base)", stock.get_nearest_business_day_in_a_week, args.base_date)
    if T is None:
        print("KRX 접근 실패 — 이후 단계 진행 불가", file=sys.stderr)
        return _finish(summary, total_t0)
    if T == args.base_date:
        # DESIGN.md: T = 실행 시점 기준 '직전' 거래일. 실행일이 거래일이면 하루 전으로 보정.
        prev = dt.datetime.strptime(T, "%Y%m%d").date() - dt.timedelta(days=1)
        T = timed("get_nearest_business_day_in_a_week(base-1)", stock.get_nearest_business_day_in_a_week, ymd(prev))
    summary["T"] = T
    Td = dt.datetime.strptime(T, "%Y%m%d").date()
    start_1w = timed("nearest_bday(T-7d)", stock.get_nearest_business_day_in_a_week, ymd(Td - dt.timedelta(days=7)))
    start_1y = timed("nearest_bday(T-1y)", stock.get_nearest_business_day_in_a_week, ymd(Td.replace(year=Td.year - 1)))
    summary.update(start_1w=start_1w, start_1y=start_1y)

    # ── 2. 유니버스 ───────────────────────────────────────────────
    kospi = timed("get_market_ticker_list(T, KOSPI)", stock.get_market_ticker_list, T, market="KOSPI")
    kosdaq = timed("get_market_ticker_list(T, KOSDAQ)", stock.get_market_ticker_list, T, market="KOSDAQ")
    etf = timed("get_etf_ticker_list(T)", stock.get_etf_ticker_list, T)
    summary.update(n_kospi=len(kospi or []), n_kosdaq=len(kosdaq or []), n_etf=len(etf or []),
                   etf_in_kospi_list=len(set(kospi or []) & set(etf or [])))

    # ── 3. 지수 ───────────────────────────────────────────────────
    for code in ("1001", "2001"):
        idx = timed(f"get_index_ohlcv({start_1y}, {T}, {code})", stock.get_index_ohlcv, start_1y, T, code)
        if idx is not None and len(idx):
            summary[f"index_{code}_first_last"] = [str(idx.index[0].date()), str(idx.index[-1].date())]

    # ── 4. 시총·거래대금 ─────────────────────────────────────────
    cap = timed("get_market_cap(T, KOSPI)", stock.get_market_cap, T, market="KOSPI")
    if cap is not None:
        summary["market_cap_columns"] = list(cap.columns)

    # ── 5. 전종목 등락률 ─────────────────────────────────────────
    pc_1w = timed(f"get_market_price_change({start_1w}, {T}, KOSPI)", stock.get_market_price_change, start_1w, T, market="KOSPI")
    timed(f"get_market_price_change({start_1w}, {T}, KOSDAQ)", stock.get_market_price_change, start_1w, T, market="KOSDAQ")
    if not args.skip_1y:
        timed(f"get_market_price_change({start_1y}, {T}, KOSPI)", stock.get_market_price_change, start_1y, T, market="KOSPI")
    if pc_1w is not None:
        summary["price_change_columns"] = list(pc_1w.columns)

    # ── 6. 액면분할 검증 ─────────────────────────────────────────
    sd = dt.datetime.strptime(args.split_date, "%Y%m%d").date()
    f = timed("nearest_bday(split-40d)", stock.get_nearest_business_day_in_a_week, ymd(sd - dt.timedelta(days=40)))
    t_end = min(sd + dt.timedelta(days=30), Td)
    t = timed("nearest_bday(split+30d)", stock.get_nearest_business_day_in_a_week, ymd(t_end))
    tk = args.split_ticker
    split: dict = {"ticker": tk, "split_date": args.split_date, "window": [f, t]}

    pc_adj = timed(f"price_change({f},{t},{args.market},adjusted=True)", stock.get_market_price_change, f, t, market=args.market, adjusted=True)
    pc_raw = timed(f"price_change({f},{t},{args.market},adjusted=False)", stock.get_market_price_change, f, t, market=args.market, adjusted=False)
    oh_adj = timed(f"get_market_ohlcv({f},{t},{tk},adjusted=True)", stock.get_market_ohlcv, f, t, tk, adjusted=True)
    oh_raw = timed(f"get_market_ohlcv({f},{t},{tk},adjusted=False)", stock.get_market_ohlcv, f, t, tk, adjusted=False)

    def row(df, label):
        if df is None or tk not in df.index:
            split[label] = None
            return None
        r = df.loc[tk]
        d = {"기준가(시가)": float(r["시가"]), "종가": float(r["종가"]), "등락률": float(r["등락률"])}
        split[label] = d
        return d

    a = row(pc_adj, "price_change_adjusted")
    r = row(pc_raw, "price_change_raw")

    def ret_from_ohlcv(df, label):
        if df is None or df.empty:
            split[label] = None
            return None
        c0, c1 = float(df["종가"].iloc[0]), float(df["종가"].iloc[-1])
        d = {"first_date": str(df.index[0].date()), "first_close": c0,
             "last_date": str(df.index[-1].date()), "last_close": c1,
             "ret_pct": round((c1 / c0 - 1) * 100, 2)}
        split[label] = d
        return d

    oa = ret_from_ohlcv(oh_adj, "ohlcv_adjusted")
    orw = ret_from_ohlcv(oh_raw, "ohlcv_raw")

    verdict = "UNKNOWN"
    if a and oa:
        diff_adj = abs(a["등락률"] - oa["ret_pct"])
        split["diff_vs_ohlcv_adjusted_pp"] = round(diff_adj, 2)
        if orw:
            split["diff_vs_ohlcv_raw_pp"] = round(abs(a["등락률"] - orw["ret_pct"]), 2)
        # 10:1 분할이면 미반영 시 등락률 ≈ -90%. 수정주가 기준 수익률과 1%p 이내면 반영으로 판정.
        verdict = "ADJUSTED" if diff_adj < 1.0 else "NOT_ADJUSTED"
        # '시가' 컬럼 의미 확인: close(start) 인지 close(start 전일) 인지
        split["base_price_equals_first_close"] = abs(a["기준가(시가)"] - oa["first_close"]) < 1e-6
    split["verdict"] = verdict
    summary["split_check"] = split
    print("\n=== 액면분할 검증 ===")
    print(json.dumps(split, ensure_ascii=False, indent=2))
    return _finish(summary, total_t0)


def _finish(summary: dict, total_t0: float) -> int:
    summary["total_sec"] = round(time.perf_counter() - total_t0, 1)
    summary["calls"] = results
    RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
    RESULT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n| call | ok | sec | rows |\n|---|---|---|---|")
    for r in results:
        print(f"| {r['call']} | {r['ok']} | {r['sec']} | {r['rows']} |")
    print(f"\ntotal {summary['total_sec']}s → {RESULT_JSON}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
