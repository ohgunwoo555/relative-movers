"""src/calc.py — 가짜 데이터로 DESIGN.md 2절 공식과 6절 흐름(상장폐지·신규상장·거래정지·거래대금 하한) 검증."""
from __future__ import annotations

import logging

import pandas as pd
import pytest

from src.calc import CALC_COLUMNS, CalcError, compute, market_return, stock_returns, trading_days_in_window
from src.calendar import PeriodWindow
from src.universe import listed_frame

T, FROM, BASE = "20260907", "20260907", "20260904"          # 1d: from = T, 기준가일 = 9/4(금)
W1D = PeriodWindow("1d", T, T, FROM, BASE)
CONFIG = {"exclude": {"suspended": True}, "filters": {"min_avg_trading_value": 0}}


def index_df(base=100.0, end=102.0):
    idx = pd.DatetimeIndex(pd.to_datetime([BASE, T], format="%Y%m%d"), name="날짜")
    return pd.DataFrame({"시가": [base, end], "고가": [0, 0], "저가": [0, 0], "종가": [base, end], "거래량": [1, 1]}, index=idx)


def price_change():
    # pykrx get_market_price_change(from=T, T): 시가 = 기준가(9/4 종가), 종가 = 9/7 종가
    rows = {
        "AAA000": ("에이", 100.0, 110.0, 10.0, 1000, 5_000_000_000),   # +10% → excess +8
        "BBB000": ("비",   100.0,  95.0, -5.0, 1000, 2_000_000_000),   # -5%  → excess -7
        "CCC000": ("씨",   200.0, 204.0,  2.0, 1000,   500_000_000),   # +2%  → excess 0
        "EEE000": ("정지", 100.0, 100.0,  0.0,    0,             0),   # 거래량 0 → 거래정지 제외
        "FFF005": ("에프우", 50.0, 60.0, 20.0, 1000, 1_000_000_000),   # 우선주 → 유니버스에 없음
        "ZZZ000": ("폐지",  10.0,   0.0, -100.0,  0,             0),   # 상장폐지 -100 행
    }
    df = pd.DataFrame([(k, *v) for k, v in rows.items()],
                      columns=["티커", "종목명", "시가", "종가", "등락률", "거래량", "거래대금"]).set_index("티커")
    df["변동폭"] = df["종가"] - df["시가"]
    return df


def universe():
    # 신규상장 NNN000 은 T 유니버스에 있지만 price_change 에 없다 (시작일 데이터 없음)
    return listed_frame({"AAA000": "에이", "BBB000": "비", "CCC000": "씨", "EEE000": "정지", "NNN000": "신규"})


def market_cap():
    return pd.DataFrame({"종가": [110, 95, 204, 100], "시가총액": [1e12, 5e11, 2e12, 1e11],
                         "거래량": [1, 1, 1, 0], "거래대금": [1, 1, 1, 0], "상장주식수": [1, 1, 1, 1]},
                        index=pd.Index(["AAA000", "BBB000", "CCC000", "EEE000"], name="티커"))


# ── 시장 수익률 ──────────────────────────────────────────────────────────
def test_market_return_uses_base_date_and_T():
    mr = market_return(index_df(100, 102), W1D, "KOSPI")
    assert (mr.base_close, mr.end_close) == (100.0, 102.0) and mr.ret_pct == pytest.approx(2.0)


def test_market_return_requires_both_dates():
    idx = index_df().iloc[[1]]                              # 기준가일 행 없음
    with pytest.raises(CalcError, match="지수 종가 누락"):
        market_return(idx, W1D, "KOSPI")
    with pytest.raises(CalcError):
        market_return(index_df(0, 1), W1D, "KOSPI")


def test_trading_days_in_window_excludes_base_date():
    assert trading_days_in_window(index_df(), W1D) == 1     # [from=T, T] → 1 거래일


# ── 종목 수익률 + 필터 ───────────────────────────────────────────────────
def test_stock_returns_drops_delisted_new_listing_and_suspended():
    sr = stock_returns(price_change(), universe(), CONFIG, n_trading_days=1)
    assert list(sr.index) == ["AAA000", "BBB000", "CCC000"]  # ZZZ(-100)·NNN(신규)·EEE(정지)·FFF(우선주) 탈락
    assert sr.loc["AAA000", "stock_ret"] == pytest.approx(10.0)
    assert sr.loc["BBB000", "stock_ret"] == pytest.approx(-5.0)
    assert sr.loc["CCC000", "stock_ret"] == pytest.approx(2.0)


def test_stock_returns_keeps_suspended_when_config_off():
    cfg = {"exclude": {"suspended": False}, "filters": {}}
    sr = stock_returns(price_change(), universe(), cfg, 1)
    assert "EEE000" in sr.index and "ZZZ000" not in sr.index   # 상장폐지는 config 와 무관하게 제거


def test_min_avg_trading_value_filter():
    cfg = {"exclude": {"suspended": True}, "filters": {"min_avg_trading_value": 1_000_000_000}}
    sr = stock_returns(price_change(), universe(), cfg, n_trading_days=1)
    assert list(sr.index) == ["AAA000", "BBB000"]               # CCC 5억 < 10억 탈락
    # 거래일 5일이면 평균 = 누적/5 → AAA 10억, BBB 4억 → AAA 만 통과
    sr5 = stock_returns(price_change(), universe(), cfg, n_trading_days=5)
    assert list(sr5.index) == ["AAA000"]


def test_stock_returns_warns_when_krx_ret_disagrees(caplog):
    pc = price_change()
    pc.loc["AAA000", "등락률"] = 12.0                         # 권리락 등으로 KRX 등락률이 다르다고 가정
    with caplog.at_level(logging.WARNING, logger="src.calc"):
        stock_returns(pc, universe(), CONFIG, 1)
    assert any("KRX 등락률과" in r.message for r in caplog.records)


# ── compute: long format ─────────────────────────────────────────────────
def test_compute_long_format_and_excess_sign():
    out = compute("KOSPI", W1D, price_change=price_change(), index_df=index_df(100, 102),
                  universe=universe(), market_cap=market_cap(), config=CONFIG)
    assert list(out.columns) == CALC_COLUMNS
    assert set(out["ticker"]) == {"AAA000", "BBB000", "CCC000"}
    row = out.set_index("ticker")
    assert row.loc["AAA000", "excess_ret"] == pytest.approx(8.0)
    assert row.loc["BBB000", "excess_ret"] == pytest.approx(-7.0)
    assert row.loc["CCC000", "excess_ret"] == pytest.approx(0.0)
    assert out["market_ret"].tolist() == pytest.approx([2.0] * 3)
    assert (out["base_date"] == "2026-09-07").all() and (out["start_date"] == "2026-09-07").all()
    assert (out["market"] == "KOSPI").all() and (out["period"] == "1d").all()
    assert row.loc["AAA000", "start_close"] == 100.0 and row.loc["AAA000", "end_close"] == 110.0
    assert row.loc["AAA000", "market_cap"] == 1e12 and row.loc["AAA000", "trading_value"] == 5_000_000_000
    assert row.loc["AAA000", "name"] == "에이"


def test_compute_without_market_cap():
    out = compute("KOSDAQ", W1D, price_change=price_change(), index_df=index_df(),
                  universe=universe(), market_cap=None, config=CONFIG)
    assert out["market_cap"].isna().all()


def test_compute_negative_market_return_flips_sign():
    # 시장 -3%, 종목 -1% → 초과수익률 +2
    out = compute("KOSPI", W1D, price_change=price_change().assign(종가=lambda d: d["시가"] * 0.99),
                  index_df=index_df(100, 97), universe=universe(), market_cap=None, config=CONFIG)
    assert out["excess_ret"].tolist() == pytest.approx([2.0] * len(out))
