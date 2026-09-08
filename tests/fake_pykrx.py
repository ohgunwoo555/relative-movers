"""오프라인 테스트용 가짜 pykrx (KOSPI 7종 + KOSDAQ 4종, 주말만 휴장).

`install(monkeypatch, authenticated=True)` 가 sys.modules 에 pykrx 모듈 트리를 심는다.
src.fetch.Fetcher 가 실제 import 경로(`from pykrx import stock`, `pykrx.website.comm.auth`, `pykrx.website.krx.market.core`)로 쓸 수 있다.
"""
from __future__ import annotations

import datetime as dt
import sys
import types

import pandas as pd

T, T_PREV = "20260907", "20260904"
# (티커, 종목명, 소속부, 기준가, 종가(T), 거래량, 거래대금, 시가총액)
ROWS = {
    "KOSPI": [
        ("005930", "삼성전자", "", 70000, 72100, 10_000_000, 7e11, 4.3e14),      # +3.0%
        ("000660", "SK하이닉스", "", 200000, 190000, 3_000_000, 6e11, 1.4e14),   # -5.0%
        ("005380", "현대차", "", 250000, 250000, 1_000_000, 2.5e11, 5e13),       # 0%
        ("058430", "포스코스틸리온", "", 4500, 4950, 500_000, 2e9, 2.7e11),       # +10%
        ("005935", "삼성전자우", "", 60000, 66000, 1_000_000, 6e10, 5e13),        # 우선주 → 제외
        ("437780", "삼성스팩8호", "SPAC", 2000, 2100, 100, 2e5, 1e10),            # 스팩 → 제외
        ("111110", "정지종목", "", 1000, 1000, 0, 0, 1e10),                      # 거래정지 → 제외
    ],
    "KOSDAQ": [
        ("060310", "3S", "중견기업부", 2000, 2200, 100_000, 2e8, 1e11),           # +10%
        ("054620", "APS", "우량기업부", 5000, 4750, 50_000, 2e8, 2e11),           # -5%
        ("012345", "관리코스닥", "관리종목(소속부없음)", 1000, 1500, 10, 1e4, 1e9),  # 관리종목 → 제외
        ("900250", "크리스탈신소재", "외국기업(소속부없음)", 3000, 3030, 1000, 3e6, 1e10),  # +1%
    ],
}
DELISTED = {"KOSPI": ("999990", "폐지종목", "", 500, 0, 0, 0, 0), "KOSDAQ": ("888880", "폐지코스닥", "", 700, 0, 0, 0, 0)}
NEW_LISTING = {"KOSPI": ("222220", "신규상장", ""), "KOSDAQ": ("333330", "신규코스닥", "벤처기업부")}
INDEX_RET = {"1001": 0.01, "2001": -0.02}      # KOSPI +1%, KOSDAQ -2%


def bday(date, prev=True):
    d = dt.datetime.strptime(date, "%Y%m%d").date()
    step = dt.timedelta(days=-1 if prev else 1)
    while d.weekday() >= 5:
        d += step
    return d.strftime("%Y%m%d")


class FakeStock:
    def __init__(self):
        self.calls: list[tuple] = []

    def get_nearest_business_day_in_a_week(self, date, prev=True):
        self.calls.append(("nearest", date, prev)); return bday(date, prev)

    def get_etf_ticker_list(self, date):
        self.calls.append(("etf", date)); return ["069500"]

    def get_etn_ticker_list(self, date):
        self.calls.append(("etn", date)); return ["500001"]

    def get_index_ohlcv(self, f, t, code):
        self.calls.append(("index", f, t, code))
        days = []
        d, end = dt.datetime.strptime(f, "%Y%m%d").date(), dt.datetime.strptime(t, "%Y%m%d").date()
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d += dt.timedelta(days=1)
        n = len(days)
        ret = INDEX_RET[code]
        closes = [3000.0 * (1 + ret * i / (n - 1)) for i in range(n)] if n > 1 else [3000.0]
        return pd.DataFrame({"시가": 1, "고가": 1, "저가": 1, "종가": closes, "거래량": 1},
                            index=pd.DatetimeIndex(pd.to_datetime(days), name="날짜"))

    def get_market_cap(self, date, market="KOSPI"):
        self.calls.append(("cap", date, market))
        rows = ROWS[market]
        return pd.DataFrame({"종가": [r[4] for r in rows], "시가총액": [r[7] for r in rows], "거래량": 1, "거래대금": 1, "상장주식수": 1},
                            index=pd.Index([r[0] for r in rows], name="티커"))

    def get_market_price_change(self, f, t, market="KOSPI", adjusted=True):
        self.calls.append(("price_change", f, t, market, adjusted))
        rows = ROWS[market] + [DELISTED[market]]
        return pd.DataFrame({"종목명": [r[1] for r in rows], "시가": [r[3] for r in rows], "종가": [r[4] for r in rows],
                             "변동폭": [r[4] - r[3] for r in rows],
                             "등락률": [round((r[4] / r[3] - 1) * 100, 2) if r[4] else -100.0 for r in rows],
                             "거래량": [r[5] for r in rows], "거래대금": [r[6] for r in rows]},
                            index=pd.Index([r[0] for r in rows], name="티커"))

    def get_market_ohlcv(self, f, t, ticker, adjusted=True):
        self.calls.append(("ohlcv", f, t, ticker, adjusted))
        idx = pd.DatetimeIndex(pd.to_datetime([T_PREV, T], format="%Y%m%d"), name="날짜")
        return pd.DataFrame({"종가": [4500.0, 4510.0]}, index=idx)


class Fake전종목시세:
    def fetch(self, trdDd, mktId):
        market = "KOSPI" if mktId == "STK" else "KOSDAQ"
        rows = [(r[0], r[1], r[2]) for r in ROWS[market]] + [NEW_LISTING[market]]
        return pd.DataFrame(rows, columns=["ISU_SRT_CD", "ISU_ABBRV", "SECT_TP_NM"])


def install(monkeypatch, authenticated: bool = True) -> FakeStock:
    stock = FakeStock()
    core = types.ModuleType("pykrx.website.krx.market.core"); core.전종목시세 = Fake전종목시세
    market = types.ModuleType("pykrx.website.krx.market"); market.core = core
    krx = types.ModuleType("pykrx.website.krx"); krx.market = market
    auth = types.ModuleType("pykrx.website.comm.auth")
    auth.get_auth_session = lambda: types.SimpleNamespace(is_authenticated=True) if authenticated else None
    comm = types.ModuleType("pykrx.website.comm"); comm.auth = auth
    website = types.ModuleType("pykrx.website"); website.krx = krx; website.comm = comm
    pk = types.ModuleType("pykrx"); pk.stock = stock; pk.website = website; pk.__version__ = "fake"
    for name, mod in [("pykrx", pk), ("pykrx.website", website), ("pykrx.website.krx", krx),
                      ("pykrx.website.krx.market", market), ("pykrx.website.krx.market.core", core),
                      ("pykrx.website.comm", comm), ("pykrx.website.comm.auth", auth), ("pykrx.stock", stock)]:
        monkeypatch.setitem(sys.modules, name, mod)
    # 네트워크·전역 패치·sleep 차단
    import src.fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "install_default_timeout", lambda *_: None)
    monkeypatch.setattr(fetch_mod, "purge_pykrx_modules", lambda: 0)
    monkeypatch.setattr(fetch_mod.Fetcher, "_kind_administrative_codes", lambda self, T: None)
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda *_: None)
    return stock
