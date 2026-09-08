#!/usr/bin/env python3
"""DESIGN.md 10절 1단계: 환경·데이터 검증 스크립트.

검증 항목
  1. pykrx 5종 함수(DESIGN.md 4절) 실제 호출 → 동작 여부·소요시간 측정
  2. DESIGN.md 2절 기간 정의 `[from, T]` 실측:
       from = 기준 날짜 이후 가장 가까운 거래일 (1d는 from = T)
       기준가 = from 직전 거래일 종가 = get_market_price_change(from, T)['시가']
       수익률 = close(T) / 기준가 - 1
     → 1d 구간(from = T)에서 '시가' == close(T 직전 거래일) 인지 확인
  3. 최근 1년 내 액면분할 종목으로 get_market_price_change 의 수정주가 반영 여부 판정
     (종목코드는 종목명으로 교차확인)

사용:
    export KRX_ID=...; export KRX_PW=...      # pykrx>=1.2 필수 (KRX Data Marketplace 계정)
    python scripts/validate_stage1.py [--split-ticker 058430 --split-name 포스코스틸리온
                                       --split-date 20260423 --market KOSPI] [--skip-1y]

출력: stdout 표 + docs/stage1_result.json
종료코드: 0 전부 성공 / 1 일부 실패 / 2 자격증명 없음

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

SLEEP_SEC = 1
MAX_RETRIES = 3
ADJUST_TOL_PP = 1.0  # 수정주가 수익률과의 허용 오차 (%p)
ROOT = Path(__file__).resolve().parent.parent
RESULT_JSON = ROOT / "docs" / "stage1_result.json"

results: list[dict] = []
_stock = None  # 지연 import 된 pykrx.stock


# ──────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────
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
            print(f"[OK ] {label:<60} {elapsed:6.2f}s rows={n}")
            time.sleep(SLEEP_SEC)
            return out
        except Exception as e:  # noqa: BLE001
            elapsed = time.perf_counter() - t0
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[ERR] {label:<60} {elapsed:6.2f}s attempt={attempt} {last_err}")
            time.sleep(SLEEP_SEC * attempt)
    results.append({"call": label, "ok": False, "sec": None, "rows": None,
                    "attempts": MAX_RETRIES, "error": last_err})
    return None


def ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def to_date(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%Y%m%d").date()


def minus_months(d: dt.date, months: int) -> dt.date:
    """달력 기준 N개월 전. 말일 초과 시 그 달 말일로 보정."""
    y, m = d.year, d.month - months
    while m <= 0:
        y -= 1
        m += 12
    import calendar as _cal
    return d.replace(year=y, month=m, day=min(d.day, _cal.monthrange(y, m)[1]))


def bday_on_or_after(date: str) -> str | None:
    """기준 날짜 이후 가장 가까운 거래일 (DESIGN.md 2절 from)."""
    return timed(f"nearest_bday({date}, prev=False)", _stock.get_nearest_business_day_in_a_week, date, prev=False)


def bday_on_or_before(date: str) -> str | None:
    return timed(f"nearest_bday({date})", _stock.get_nearest_business_day_in_a_week, date)


def prev_bday(date: str) -> str | None:
    """date 직전 거래일 (date 자신은 제외)."""
    return bday_on_or_before(ymd(to_date(date) - dt.timedelta(days=1)))


def close_on(df, date: str):
    """OHLCV DataFrame 에서 date(YYYYMMDD)의 종가. 없으면 None."""
    if df is None or df.empty:
        return None
    ts = df.index[df.index.strftime("%Y%m%d") == date]
    return float(df.loc[ts[0], "종가"]) if len(ts) else None


def pct(a: float, b: float) -> float:
    """a / b - 1 (%)"""
    return round((a / b - 1) * 100, 2)


# ──────────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────────
def main() -> int:
    global _stock
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-date", default=ymd(dt.date.today()), help="실행일 YYYYMMDD (기본 오늘)")
    ap.add_argument("--split-ticker", default="058430", help="최근 1년 내 액면분할 종목코드 (기본 포스코스틸리온)")
    ap.add_argument("--split-name", default="포스코스틸리온", help="종목명 (종목코드 교차확인용)")
    ap.add_argument("--split-date", default="20260423", help="분할 신주 변경상장일 YYYYMMDD")
    ap.add_argument("--market", default="KOSPI", choices=["KOSPI", "KOSDAQ"], help="분할 종목의 시장")
    ap.add_argument("--ref-ticker", default="005930", help="1d 정의 실측용 종목 (기본 삼성전자)")
    ap.add_argument("--skip-1y", action="store_true", help="1년치 price_change 호출 생략(시간 절약)")
    args = ap.parse_args()

    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        print("KRX_ID / KRX_PW 환경변수가 없습니다. pykrx>=1.2 는 KRX Data Marketplace 로그인이 필요합니다.",
              file=sys.stderr)
        return 2

    total_t0 = time.perf_counter()
    summary: dict = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                     "args": {k: v for k, v in vars(args).items()}}

    # ── 0. 지연 import (DESIGN.md 4절) ────────────────────────────────
    # pykrx>=1.2 는 import 시점에 KRX 로그인을 수행하며, KRX 에 닿지 못하면 예외를 그대로 던진다.
    t0 = time.perf_counter()
    try:
        import pykrx
        from pykrx import stock
        _stock = stock
        results.append({"call": "import pykrx (KRX login at import)", "ok": True,
                        "sec": round(time.perf_counter() - t0, 2), "rows": None, "attempts": 1})
        summary["pykrx_version"] = getattr(pykrx, "__version__", "?")
    except Exception as e:  # noqa: BLE001
        results.append({"call": "import pykrx (KRX login at import)", "ok": False,
                        "sec": round(time.perf_counter() - t0, 2), "rows": None, "attempts": 1,
                        "error": f"{type(e).__name__}: {str(e)[:300]}"})
        print(f"[ERR] pykrx import/login 실패: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return _finish(summary, total_t0)

    # ── 1. T 및 구간 시작일 from (DESIGN.md 2절) ──────────────────────
    T = bday_on_or_before(args.base_date)
    if T is None:
        print("KRX 접근 실패 — 이후 단계 진행 불가", file=sys.stderr)
        return _finish(summary, total_t0)
    if T == args.base_date:
        # T = 실행 시점 기준 '직전' 거래일. 실행일이 거래일이면 하루 전으로 보정.
        T = prev_bday(T)
        if T is None:
            return _finish(summary, total_t0)
    Td = to_date(T)
    summary["T"] = T

    ref_dates = {"1d": T,
                 "1w": ymd(Td - dt.timedelta(days=7)),
                 "1m": ymd(minus_months(Td, 1)),
                 "6m": ymd(minus_months(Td, 6)),
                 "1y": ymd(minus_months(Td, 12))}
    froms: dict[str, str | None] = {"1d": T}
    for p in ("1w", "1m", "6m", "1y"):
        froms[p] = bday_on_or_after(ref_dates[p])
    summary["periods"] = {p: {"ref_date": ref_dates[p], "from": froms[p]} for p in froms}
    from_1w, from_1y = froms["1w"], froms["1y"]

    # ── 2. 유니버스 ───────────────────────────────────────────────────
    kospi = timed("get_market_ticker_list(T, KOSPI)", stock.get_market_ticker_list, T, market="KOSPI")
    kosdaq = timed("get_market_ticker_list(T, KOSDAQ)", stock.get_market_ticker_list, T, market="KOSDAQ")
    etf = timed("get_etf_ticker_list(T)", stock.get_etf_ticker_list, T)
    summary.update(n_kospi=len(kospi or []), n_kosdaq=len(kosdaq or []), n_etf=len(etf or []),
                   etf_in_kospi_list=len(set(kospi or []) & set(etf or [])))

    # ── 3. 지수: idx(T) / idx(from 직전 거래일) - 1 ──────────────────
    idx_start = prev_bday(from_1w) if from_1w else None
    summary["index_1w"] = {}
    for code in ("1001", "2001"):
        if idx_start is None:
            break
        idx = timed(f"get_index_ohlcv({idx_start}, {T}, {code})", stock.get_index_ohlcv, idx_start, T, code)
        c0, c1 = close_on(idx, idx_start), close_on(idx, T)
        summary["index_1w"][code] = {"base_date": idx_start, "base": c0, "T": T, "close_T": c1,
                                     "ret_pct": pct(c1, c0) if c0 and c1 else None}
    if not args.skip_1y and from_1y:
        s1y = prev_bday(from_1y)
        if s1y:
            timed(f"get_index_ohlcv({s1y}, {T}, 1001) [1y]", stock.get_index_ohlcv, s1y, T, "1001")

    # ── 4. 시총·거래대금 ─────────────────────────────────────────────
    cap = timed("get_market_cap(T, KOSPI)", stock.get_market_cap, T, market="KOSPI")
    if cap is not None:
        summary["market_cap_columns"] = list(cap.columns)

    # ── 5. 전종목 등락률 (1d / 1w / 1y) ──────────────────────────────
    pc_1d = timed(f"get_market_price_change({T}, {T}, KOSPI) [1d]", stock.get_market_price_change, T, T, market="KOSPI")
    pc_1w = timed(f"get_market_price_change({from_1w}, {T}, KOSPI) [1w]", stock.get_market_price_change, from_1w, T, market="KOSPI")
    timed(f"get_market_price_change({from_1w}, {T}, KOSDAQ) [1w]", stock.get_market_price_change, from_1w, T, market="KOSDAQ")
    if not args.skip_1y and from_1y:
        timed(f"get_market_price_change({from_1y}, {T}, KOSPI) [1y]", stock.get_market_price_change, from_1y, T, market="KOSPI")
    if pc_1w is not None:
        summary["price_change_columns"] = list(pc_1w.columns)

    # ── 6. 기간 정의 실측: 1d 구간에서 '시가' == close(T 직전 거래일) ──
    summary["definition_check"] = _definition_check(stock, pc_1d, pc_1w, T, from_1w, args.ref_ticker)

    # ── 7. 액면분할 검증 ─────────────────────────────────────────────
    summary["split_check"] = _split_check(stock, args, T, kospi, kosdaq)

    return _finish(summary, total_t0)


def _definition_check(stock, pc_1d, pc_1w, T: str, from_1w: str | None, ref: str) -> dict:
    """DESIGN.md 2절: 기준가 = from 직전 거래일 종가, 수익률 = close(T)/기준가 - 1 을 실측."""
    out: dict = {"ticker": ref, "verdict": "UNKNOWN"}
    T_prev = prev_bday(T)
    if T_prev is None:
        return out
    # KRX 원시 종가 (adjusted=False 는 KRX 소스) — 1d 구간엔 권리락이 없다고 가정
    oh = timed(f"get_market_ohlcv({T_prev}, {T}, {ref}, adjusted=False)", stock.get_market_ohlcv, T_prev, T, ref, adjusted=False)
    c_prev, c_T = close_on(oh, T_prev), close_on(oh, T)
    out.update(T=T, T_prev=T_prev, close_T_prev=c_prev, close_T=c_T)
    if pc_1d is not None and ref in pc_1d.index and c_prev and c_T:
        r = pc_1d.loc[ref]
        out["pc_1d"] = {"시가(기준가)": float(r["시가"]), "종가": float(r["종가"]), "등락률": float(r["등락률"])}
        out["base_price_matches_prev_close"] = abs(float(r["시가"]) - c_prev) < 1e-6
        out["close_matches_T"] = abs(float(r["종가"]) - c_T) < 1e-6
        out["ret_matches_formula"] = abs(float(r["등락률"]) - pct(c_T, c_prev)) < 0.05
        out["verdict"] = "OK" if (out["base_price_matches_prev_close"] and out["close_matches_T"]
                                  and out["ret_matches_formula"]) else "MISMATCH"
    # 1w 도 동일하게 기준가 = from 직전 거래일 종가인지 확인
    if pc_1w is not None and from_1w and ref in pc_1w.index:
        f_prev = prev_bday(from_1w)
        if f_prev:
            oh_w = timed(f"get_market_ohlcv({f_prev}, {from_1w}, {ref}, adjusted=False)", stock.get_market_ohlcv, f_prev, from_1w, ref, adjusted=False)
            c_fprev = close_on(oh_w, f_prev)
            base_w = float(pc_1w.loc[ref, "시가"])
            out["pc_1w"] = {"from": from_1w, "from_prev": f_prev, "시가(기준가)": base_w, "close_from_prev": c_fprev,
                            "base_price_matches_prev_close": (abs(base_w - c_fprev) < 1e-6) if c_fprev else None}
    print("\n=== 기간 정의 실측 (1d: from = T, 기준가 = close(T 직전 거래일)) ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def _split_check(stock, args, T: str, kospi, kosdaq) -> dict:
    """분할 전후를 포함하는 구간 [from, to] 에서 수정주가 반영 여부 판정.

    A = price_change(adjusted=True)['등락률']
    B = price_change(adjusted=False)['등락률']   (미반영 기대값: 10:1 분할이면 ≈ -90%)
    C = ohlcv(adjusted=True, 네이버): close(to)/close(from 직전 거래일) - 1   ← 정답(수정주가)
    D = ohlcv(adjusted=False, KRX):   동일 계산
    판정: |A - C| < ADJUST_TOL_PP → ADJUSTED, 아니면 NOT_ADJUSTED
    """
    out: dict = {"ticker": args.split_ticker, "name_expected": args.split_name,
                 "split_date": args.split_date, "market": args.market, "verdict": "UNKNOWN"}
    tk = args.split_ticker

    # 종목코드 ↔ 종목명 교차확인 (+ 시장 소속 확인)
    name = timed(f"get_market_ticker_name({tk})", stock.get_market_ticker_name, tk)
    out["name_resolved"] = name
    universe = {"KOSPI": kospi, "KOSDAQ": kosdaq}[args.market]
    out["ticker_in_market_list"] = (tk in universe) if universe else None
    if name and args.split_name and name.strip() != args.split_name.strip():
        out["name_mismatch"] = True
        # 종목명으로 코드 탐색: T 시점 해당 시장 전종목 등락률에서 찾는다
        pc_T = timed(f"get_market_price_change({T}, {T}, {args.market}) [name lookup]",
                     stock.get_market_price_change, T, T, market=args.market)
        if pc_T is not None:
            hit = pc_T[pc_T["종목명"].str.strip() == args.split_name.strip()]
            if len(hit) == 1:
                tk = str(hit.index[0])
                out["ticker_resolved_by_name"] = tk
                print(f"[WARN] 종목코드 {args.split_ticker} 의 종목명은 '{name}' — '{args.split_name}' 의 코드 {tk} 로 대체하여 검증")
            else:
                out["verdict"] = "TICKER_MISMATCH"
                print(f"[WARN] 종목코드 {args.split_ticker} = '{name}', '{args.split_name}' 을 {args.market} 에서 찾지 못함 → 검증 중단")
                return out
    else:
        out["name_mismatch"] = False
    out["ticker"] = tk

    # 구간: from = (분할상장일 - 40일) 이후 가장 가까운 거래일, to = min(분할상장일 + 30일, T) 이전 가장 가까운 거래일
    sd = to_date(args.split_date)
    f = bday_on_or_after(ymd(sd - dt.timedelta(days=40)))
    t = bday_on_or_before(ymd(min(sd + dt.timedelta(days=30), to_date(T))))
    if not (f and t):
        return out
    f_prev = prev_bday(f)
    out["window"] = {"from": f, "from_prev": f_prev, "to": t}

    pc_adj = timed(f"price_change({f},{t},{args.market},adjusted=True)", stock.get_market_price_change, f, t, market=args.market, adjusted=True)
    pc_raw = timed(f"price_change({f},{t},{args.market},adjusted=False)", stock.get_market_price_change, f, t, market=args.market, adjusted=False)
    # 기준가(from 직전 거래일 종가)를 얻기 위해 from 보다 앞에서 조회
    f_back = ymd(to_date(f) - dt.timedelta(days=14))
    oh_adj = timed(f"get_market_ohlcv({f_back},{t},{tk},adjusted=True) [naver]", stock.get_market_ohlcv, f_back, t, tk, adjusted=True)
    oh_raw = timed(f"get_market_ohlcv({f_back},{t},{tk},adjusted=False) [krx]", stock.get_market_ohlcv, f_back, t, tk, adjusted=False)

    def pc_row(df, label):
        if df is None or tk not in df.index:
            out[label] = None
            return None
        r = df.loc[tk]
        d = {"종목명": str(r["종목명"]), "시가(기준가)": float(r["시가"]), "종가": float(r["종가"]), "등락률": float(r["등락률"])}
        out[label] = d
        return d

    def oh_ret(df, label):
        c0, c1 = close_on(df, f_prev) if f_prev else None, close_on(df, t)
        d = {"base_date": f_prev, "base_close": c0, "to": t, "to_close": c1,
             "ret_pct": pct(c1, c0) if c0 and c1 else None}
        out[label] = d
        return d

    A = pc_row(pc_adj, "price_change_adjusted")
    pc_row(pc_raw, "price_change_raw")
    C = oh_ret(oh_adj, "ohlcv_adjusted_naver")
    D = oh_ret(oh_raw, "ohlcv_raw_krx")

    if A and C and C["ret_pct"] is not None:
        diff = abs(A["등락률"] - C["ret_pct"])
        out["diff_A_vs_C_pp"] = round(diff, 2)
        if D and D["ret_pct"] is not None:
            out["diff_A_vs_D_pp"] = round(abs(A["등락률"] - D["ret_pct"]), 2)
        out["base_price_matches_adjusted_prev_close"] = abs(A["시가(기준가)"] - C["base_close"]) < 1e-6 if C["base_close"] else None
        out["verdict"] = "ADJUSTED" if diff < ADJUST_TOL_PP else "NOT_ADJUSTED"
        # 분할이 실제로 구간 안에 있었는지 (원시 종가 급락) 보조 확인
        if D and D["ret_pct"] is not None and C["ret_pct"] is not None:
            out["raw_vs_adjusted_gap_pp"] = round(abs(D["ret_pct"] - C["ret_pct"]), 2)
            if out["raw_vs_adjusted_gap_pp"] < 5:
                out["warning"] = "원시/수정 수익률 차이가 작음 — 구간에 분할이 없거나 분할 종목·날짜가 잘못됐을 수 있음"

    print("\n=== 액면분할 검증 ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


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
