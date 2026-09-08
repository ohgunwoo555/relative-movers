"""수익률·초과수익률 계산 — DESIGN.md 2절·6절·7절.

한 (시장, 기간) 조합에 대해:
  1. 시장 수익률  market_ret = idx(T) / idx(base_date) - 1           (base_date = from 직전 거래일)
  2. 종목 수익률  stock_ret  = close(T) / 기준가 - 1                  (기준가 = price_change 의 '시가')
  3. 유니버스 inner join (상장폐지 -100 행·신규상장 탈락)
  4. 거래정지 제외 (구간 거래량 0, config exclude.suspended)
  5. 거래대금 하한 (기간 평균 거래대금 < filters.min_avg_trading_value 제외)
  6. 초과수익률   excess_ret = stock_ret - market_ret (%p)

출력은 DESIGN.md 7절 long-format 컬럼에서 direction/rank 를 뺀 프레임이다 (rank.py 가 채운다).
이 모듈은 네트워크를 모른다 — 입력 프레임은 fetch.py 가 만든다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from src.calendar import PeriodWindow, to_iso
from src.universe import apply_suspended, inner_join_universe

log = logging.getLogger(__name__)

RESULT_COLUMNS = ["base_date", "market", "period", "direction", "rank", "ticker", "name",
                  "start_date", "start_close", "end_close", "stock_ret", "market_ret", "excess_ret",
                  "market_cap", "trading_value"]
CALC_COLUMNS = [c for c in RESULT_COLUMNS if c not in ("direction", "rank")]


class CalcError(ValueError):
    """입력 프레임이 정의와 맞지 않는다 (지수 기준일 불일치 등)."""


@dataclass(frozen=True)
class MarketReturn:
    market: str
    period: str
    base_date: str      # 기준가일 (YYYYMMDD)
    T: str
    base_close: float
    end_close: float

    @property
    def ret_pct(self) -> float:
        return (self.end_close / self.base_close - 1) * 100


def index_close_on(index_df: pd.DataFrame, date: str) -> float | None:
    """`get_index_ohlcv` 결과(DatetimeIndex)에서 date(YYYYMMDD)의 종가."""
    if index_df is None or index_df.empty:
        return None
    hit = index_df.index[index_df.index.strftime("%Y%m%d") == date]
    return float(index_df.loc[hit[0], "종가"]) if len(hit) else None


def market_return(index_df: pd.DataFrame, window: PeriodWindow, market: str) -> MarketReturn:
    """DESIGN.md 2절: idx(T) / idx(from 직전 거래일) - 1. 두 날짜 모두 지수 데이터에 있어야 한다."""
    base = index_close_on(index_df, window.base_date)
    end = index_close_on(index_df, window.T)
    if base is None or end is None:
        have = [d.strftime("%Y%m%d") for d in index_df.index[:3]] if index_df is not None and len(index_df) else []
        raise CalcError(f"[{market}/{window.period}] 지수 종가 누락: base_date={window.base_date} T={window.T} (있는 날짜 예: {have})")
    if base <= 0:
        raise CalcError(f"[{market}/{window.period}] 지수 기준 종가가 0 이하: {base}")
    return MarketReturn(market, window.period, window.base_date, window.T, base, end)


def trading_days_in_window(index_df: pd.DataFrame, window: PeriodWindow) -> int:
    """구간 [from, T] 의 거래일 수 (지수 일봉 기준)."""
    dates = index_df.index.strftime("%Y%m%d")
    return int(((dates >= window.from_date) & (dates <= window.T)).sum())


def stock_returns(price_change: pd.DataFrame, universe: pd.DataFrame, config: Mapping,
                  n_trading_days: int) -> pd.DataFrame:
    """유니버스 join → 거래정지 제외 → 거래대금 하한 → stock_ret 계산. 반환 index=티커."""
    df = inner_join_universe(price_change, universe)
    n_joined = len(df)
    df = apply_suspended(df, config)
    n_after_susp = len(df)
    df = df[df["시가"] > 0]                                   # 기준가 0 = 데이터 없음 (0 나눗셈 방지)
    min_tv = float((config.get("filters", {}) or {}).get("min_avg_trading_value", 0) or 0)
    if min_tv > 0 and n_trading_days > 0:
        avg_tv = df["거래대금"].astype(float) / n_trading_days
        df = df[avg_tv >= min_tv]
    log.info("stock_returns: joined=%d → -suspended=%d → -min_tv=%d", n_joined, n_after_susp, len(df))
    out = pd.DataFrame(index=df.index.astype(str))
    out.index.name = "ticker"
    out["name"] = df["name"]
    out["start_close"] = df["시가"].astype(float)
    out["end_close"] = df["종가"].astype(float)
    out["stock_ret"] = (out["end_close"] / out["start_close"] - 1) * 100
    out["trading_value"] = df["거래대금"].astype(float)
    # KRX 등락률과의 정합성 확인 (권리락 등으로 '시가' 가 조정된 경우 KRX 등락률이 정답이므로 크게 다르면 경고)
    if "등락률" in df.columns:
        diff = (out["stock_ret"] - df["등락률"].astype(float)).abs()
        n_bad = int((diff > 0.05).sum())
        if n_bad:
            log.warning("stock_ret 이 KRX 등락률과 0.05%%p 초과로 다른 종목 %d개 (예: %s)", n_bad, list(diff[diff > 0.05].index[:5]))
    return out


def compute(market: str, window: PeriodWindow, *, price_change: pd.DataFrame, index_df: pd.DataFrame,
            universe: pd.DataFrame, market_cap: pd.DataFrame | None, config: Mapping) -> pd.DataFrame:
    """한 (시장, 기간) 조합의 long-format 프레임 (direction/rank 제외). 행 = 유니버스 종목."""
    mr = market_return(index_df, window, market)
    n_days = trading_days_in_window(index_df, window)
    sr = stock_returns(price_change, universe, config, n_days)
    if market_cap is not None and "시가총액" in market_cap.columns:
        caps = market_cap["시가총액"].astype(float)
        caps.index = caps.index.astype(str)
        sr["market_cap"] = caps.reindex(sr.index)
    else:
        sr["market_cap"] = float("nan")
    sr["market_ret"] = mr.ret_pct
    sr["excess_ret"] = sr["stock_ret"] - mr.ret_pct
    sr["base_date"] = to_iso(window.T)
    sr["market"] = market
    sr["period"] = window.period
    sr["start_date"] = to_iso(window.from_date)
    out = sr.reset_index()[CALC_COLUMNS]
    log.info("[%s/%s] market_ret=%.3f%% (idx %s→%s: %.2f→%.2f), rows=%d, trading_days=%d",
             market, window.period, mr.ret_pct, window.base_date, window.T, mr.base_close, mr.end_close, len(out), n_days)
    return out
