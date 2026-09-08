#!/usr/bin/env python3
"""2단계 검증: T 시점 KOSPI/KOSDAQ 유니버스를 실제로 구성해 단계별 종목 수를 출력한다.

- pykrx 로 T 시점 티커·종목명(전종목시세, 소속부 포함), ETF/ETN 목록을 받아 `src.universe.build_universe` 적용
- 1단계 실측 파일(docs/results/stage1_result.json)이 있으면 listed/ETF 개수를 대조
- 우선주 규칙(코드 끝자리 vs 종목명) 충돌 종목과 전종목기본정보(주식종류=우선주) 대조 결과 출력
- 관리종목 판별 소스 조사: (a) 전종목시세 소속부 (b) 전종목기본정보 소속부 (c) KIND 관리종목 페이지 접근 여부 (GET; POST 파싱은 3단계 fetch.py/validate_fetch)

사용:
    export KRX_ID=...; export KRX_PW=...
    python scripts/validate_universe.py [--base-date YYYYMMDD] [--stage1-json docs/stage1_result.json]
출력: stdout + docs/results/universe_result.json (+ universe_result_<T>.json)
종료코드: 0 성공 / 1 일부 실패 / 2 자격증명 없음
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.calendar import resolve_base_date  # noqa: E402
from src.universe import (  # noqa: E402
    administrative_from_sect, build_universe, listed_frame, preferred_rule_breakdown,
)

SLEEP_SEC = 1
MAX_RETRIES = 3
RESULTS_DIR = ROOT / "docs" / "results"
RESULT_JSON = RESULTS_DIR / "universe_result.json"
KIND_ADMIN_URL = "https://kind.krx.co.kr/investwarn/adminissue.do?method=searchAdminIssueMain"

results: list[dict] = []
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def timed(label: str, fn, *args, **kwargs):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
            el = time.perf_counter() - t0
            n = len(out) if hasattr(out, "__len__") else None
            results.append({"call": label, "ok": True, "sec": round(el, 2), "rows": n})
            print(f"[OK ] {label:<55} {el:6.2f}s rows={n}")
            time.sleep(SLEEP_SEC)
            return out
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[ERR] {label:<55} attempt={attempt} {last_err}")
            time.sleep(SLEEP_SEC * attempt)
    results.append({"call": label, "ok": False, "sec": None, "rows": None, "error": last_err})
    return None


def value_counts(series, top: int = 15) -> dict:
    return {str(k): int(v) for k, v in series.fillna("").astype(str).str.strip().value_counts().head(top).items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--stage1-json", default=str(RESULTS_DIR / "stage1_result.json"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        print("KRX_ID / KRX_PW 환경변수가 없습니다.", file=sys.stderr)
        return 2

    import yaml
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    total_t0 = time.perf_counter()
    summary: dict = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "markets": {}}

    t0 = time.perf_counter()
    try:
        from pykrx import stock
        from pykrx.website import krx
        from pykrx.website.krx.market.core import 전종목기본정보, 전종목시세
        results.append({"call": "import pykrx (KRX login)", "ok": True, "sec": round(time.perf_counter() - t0, 2), "rows": None})
    except Exception as e:  # noqa: BLE001
        results.append({"call": "import pykrx (KRX login)", "ok": False, "sec": round(time.perf_counter() - t0, 2),
                        "rows": None, "error": f"{type(e).__name__}: {str(e)[:300]}"})
        return _finish(summary, total_t0)

    nearest = lambda d, prev=True: stock.get_nearest_business_day_in_a_week(d, prev=prev)  # noqa: E731
    T = resolve_base_date(args.base_date, nearest)
    if T is None:
        # 검증 목적이므로 휴장일이어도 직전 거래일로 진행 (main.py 는 여기서 종료한다)
        T = nearest(args.base_date, True)
        summary["note"] = f"{args.base_date} 는 휴장일 — 검증은 T={T} 로 진행"
    summary["T"] = T
    time.sleep(SLEEP_SEC)

    # 공통 목록
    etf = timed("get_etf_ticker_list(T)", stock.get_etf_ticker_list, T)
    etn = timed("get_etn_ticker_list(T)", stock.get_etn_ticker_list, T)
    summary["n_etf"], summary["n_etn"] = len(etf or []), len(etn or [])

    # 1단계 실측 대조
    stage1 = Path(args.stage1_json)
    if stage1.exists():
        s1 = json.loads(stage1.read_text(encoding="utf-8"))
        summary["stage1_compare"] = {"stage1_T": s1.get("T"), "stage1_n_kospi": s1.get("n_kospi"),
                                     "stage1_n_kosdaq": s1.get("n_kosdaq"), "stage1_n_etf": s1.get("n_etf"),
                                     "same_T": s1.get("T") == T}
    else:
        summary["stage1_compare"] = None

    # 관리종목 소스 조사 (b): 전종목기본정보 — 날짜 인자 없음(현재 스냅샷)
    basic = timed("전종목기본정보().fetch(ALL) [MDCSTAT01901]", 전종목기본정보().fetch, "ALL")
    basic_by_market: dict = {}
    if basic is not None and len(basic):
        basic = basic.set_index("ISU_SRT_CD")
        for mkt, key in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
            sub = basic[basic["MKT_TP_NM"].astype(str).str.startswith(key)]
            basic_by_market[mkt] = sub
            summary.setdefault("basic_info", {})[mkt] = {
                "n": int(len(sub)),
                "SECT_TP_NM": value_counts(sub["SECT_TP_NM"]),
                "KIND_STKCERT_TP_NM": value_counts(sub["KIND_STKCERT_TP_NM"]),
                "SECUGRP_NM": value_counts(sub["SECUGRP_NM"]),
            }

    for market, mkt_id in (("KOSPI", "STK"), ("KOSDAQ", "KSQ")):
        m: dict = {}
        raw = timed(f"전종목시세().fetch({T}, {mkt_id}) [MDCSTAT01501]", 전종목시세().fetch, T, mkt_id)
        if raw is None:
            summary["markets"][market] = {"error": "전종목시세 실패"}
            continue
        names = raw.set_index("ISU_SRT_CD")["ISU_ABBRV"]
        sect = raw.set_index("ISU_SRT_CD")["SECT_TP_NM"]
        listed = listed_frame(names, sect)
        m["n_listed"] = int(len(listed))
        m["sect_value_counts"] = value_counts(listed["sect"])            # (a) 소속부에 '관리종목' 이 있는가
        admin_from_sect = administrative_from_sect(listed)
        m["n_administrative_from_sect"] = len(admin_from_sect or [])
        m["administrative_from_sect_sample"] = sorted(admin_from_sect or [])[:20]

        # 유니버스 구성 — 관리종목은 소속부 기반 집합을 넘긴다 (KOSPI 커버리지 확인 목적)
        res = build_universe(listed, market, config, etf_tickers=etf or [], etn_tickers=etn,
                             administrative_tickers=admin_from_sect)
        m["steps"] = res.steps
        m["warnings"] = res.warnings
        m["removed_counts"] = {k: len(v) for k, v in res.removed.items()}
        m["removed_samples"] = {k: [(t, listed.loc[t, "name"]) for t in v[:10]] for k, v in res.removed.items()}

        # 우선주 규칙 점검: 코드 규칙 vs 이름 규칙 충돌, 전종목기본정보 주식종류와 대조
        bd = preferred_rule_breakdown(listed)
        m["preferred_rule_conflicts"] = [(t, r["name"], bool(r["by_ticker"]), bool(r["by_name"])) for t, r in bd.iterrows()]
        if market in basic_by_market:
            b = basic_by_market[market]
            official_pref = set(b.index[b["KIND_STKCERT_TP_NM"].astype(str).str.contains("우선주")])
            ours = set(res.removed.get("-preferred", []))
            common = set(listed.index) & set(b.index)
            m["preferred_vs_basic_info"] = {
                "official_preferred_in_T_list": len(official_pref & common),
                "ours": len(ours & common),
                "ours_not_official": sorted((ours - official_pref) & common)[:20],
                "official_not_ours": sorted((official_pref - ours) & common)[:20],
            }
        print(res.summary())
        summary["markets"][market] = m
        time.sleep(SLEEP_SEC)

    # (c) KIND 관리종목 페이지 접근성
    try:
        import requests
        t0 = time.perf_counter()
        r = requests.get(KIND_ADMIN_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        summary["kind_admin_page"] = {"status": r.status_code, "sec": round(time.perf_counter() - t0, 2),
                                      "contains_keyword": "관리종목" in r.text, "length": len(r.text)}
    except Exception as e:  # noqa: BLE001
        summary["kind_admin_page"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    return _finish(summary, total_t0)


def _finish(summary: dict, total_t0: float) -> int:
    summary["total_sec"] = round(time.perf_counter() - total_t0, 1)
    summary["calls"] = results
    RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    RESULT_JSON.write_text(text, encoding="utf-8")
    if summary.get("T"):
        (RESULT_JSON.parent / f"universe_result_{summary['T']}.json").write_text(text, encoding="utf-8")
    print("\n=== 요약 ===")
    for mkt, m in summary.get("markets", {}).items():
        if "steps" in m:
            print(f"[{mkt}] " + " → ".join(f"{k}={v}" for k, v in m["steps"]))
            print(f"   소속부: {m['sect_value_counts']}")
            print(f"   우선주 규칙 충돌: {m['preferred_rule_conflicts'][:10]}")
            if "preferred_vs_basic_info" in m:
                print(f"   기본정보 대조: {m['preferred_vs_basic_info']}")
    print(f"KIND 관리종목 페이지: {summary.get('kind_admin_page')}")
    print(f"\ntotal {summary['total_sec']}s → {RESULT_JSON}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
