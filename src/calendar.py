"""T(기준일) 및 기간 구간 계산 — DESIGN.md 2절.

정의 (DESIGN.md 2절이 원본):
  T          실행 시점 기준 직전 거래일. 실행일이 휴장일이면 None (main.py가 즉시 종료)
  기간 구간   [from, T]
  기준 날짜   1d = T / 1w = T-7일 / 1m = T-1개월 / 6m = T-6개월 / 1y = T-1년   (config.yaml periods)
  from       기준 날짜 이후 가장 가까운 거래일 (1d는 from = T)
  기준가일    from 직전 거래일 (기준가 = 이 날의 종가. get_market_price_change의 '시가')

이 모듈은 네트워크를 모른다. 거래일 판정은 `NearestBday` 콜러블로 주입한다:
  nearest(date, prev) -> 거래일  — prev=True 면 date 이전(포함) 가장 가까운 거래일, False 면 이후(포함)
운영에서는 fetch.py 의 pykrx 래퍼(`get_nearest_business_day_in_a_week`)를, 테스트에서는 가짜 달력을 넘긴다.

날짜는 내부적으로 'YYYYMMDD' 문자열로 통일한다 (CLAUDE.md 8절).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Mapping

NearestBday = Callable[[str, bool], str]
"""(date_yyyymmdd, prev) -> 거래일 yyyymmdd. prev=True: 이전(포함) / False: 이후(포함)."""

DATE_FMT = "%Y%m%d"
ONE_DAY = dt.timedelta(days=1)


# ──────────────────────────────────────────────────────────────────────────
# 날짜 유틸
# ──────────────────────────────────────────────────────────────────────────
def parse(date: str) -> dt.date:
    """'YYYYMMDD' 또는 'YYYY-MM-DD' → date."""
    return dt.datetime.strptime(date.replace("-", ""), DATE_FMT).date()


def fmt(d: dt.date) -> str:
    return d.strftime(DATE_FMT)


def to_iso(date: str) -> str:
    """'YYYYMMDD' → 'YYYY-MM-DD' (출력 스키마용)."""
    return parse(date).isoformat()


def subtract_months(d: dt.date, months: int) -> dt.date:
    """달력 기준 N개월 전. 해당 월에 같은 일자가 없으면 그 달의 말일로 보정 (3/31 - 1개월 = 2/28)."""
    if months < 0:
        raise ValueError("months must be >= 0")
    y, m = d.year, d.month - months
    while m <= 0:
        y -= 1
        m += 12
    first_next = dt.date(y + (m // 12), (m % 12) + 1, 1)
    last_day = (first_next - ONE_DAY).day
    return dt.date(y, m, min(d.day, last_day))


def subtract_calendar(d: dt.date, *, days: int = 0, months: int = 0, years: int = 0) -> dt.date:
    """T - (years, months, days). 년·월은 말일 보정, 일은 단순 뺄셈."""
    return subtract_months(d, years * 12 + months) - dt.timedelta(days=days)


# ──────────────────────────────────────────────────────────────────────────
# 거래일 계산
# ──────────────────────────────────────────────────────────────────────────
def is_trading_day(date: str, nearest: NearestBday) -> bool:
    return nearest(date, True) == date


def prev_trading_day(date: str, nearest: NearestBday) -> str:
    """date 직전 거래일 (date 자신 제외)."""
    return nearest(fmt(parse(date) - ONE_DAY), True)


def resolve_base_date(run_date: str, nearest: NearestBday) -> str | None:
    """T = 실행일 기준 직전 거래일. 실행일이 휴장일이면 None (DESIGN.md 2절: 즉시 종료)."""
    if not is_trading_day(run_date, nearest):
        return None
    return prev_trading_day(run_date, nearest)


# ──────────────────────────────────────────────────────────────────────────
# 기간 구간
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PeriodWindow:
    period: str      # '1d' | '1w' | '1m' | '6m' | '1y'
    T: str           # 기준일
    ref_date: str    # 기준 날짜 (달력 계산 결과, 휴장일일 수 있음)
    from_date: str   # 구간 시작 거래일 = ref_date 이후 가장 가까운 거래일
    base_date: str   # 기준가일 = from_date 직전 거래일 (기준가 = 이 날 종가)

    def as_iso(self) -> dict[str, str]:
        return {"period": self.period, "base_date": to_iso(self.T),
                "start_date": to_iso(self.from_date), "base_price_date": to_iso(self.base_date)}


def reference_date(T: str, spec: Mapping) -> str:
    """config.yaml `periods.<name>` 항목 → 기준 날짜.

    kind: calendar     → T - days/months/years
    kind: trading_days → T (from 은 period_window 에서 거래일 단위로 되돌린다)
    """
    kind = spec.get("kind", "calendar")
    Td = parse(T)
    if kind == "trading_days":
        return T
    if kind == "calendar":
        return fmt(subtract_calendar(Td, days=int(spec.get("days", 0)),
                                     months=int(spec.get("months", 0)),
                                     years=int(spec.get("years", 0))))
    raise ValueError(f"unknown period kind: {kind!r}")


def period_window(period: str, T: str, spec: Mapping, nearest: NearestBday) -> PeriodWindow:
    """DESIGN.md 2절: from = 기준 날짜 이후 가장 가까운 거래일, 기준가일 = from 직전 거래일."""
    if not is_trading_day(T, nearest):
        raise ValueError(f"T={T} is not a trading day")
    ref = reference_date(T, spec)
    if spec.get("kind", "calendar") == "trading_days":
        n = int(spec.get("days", 1))
        if n < 1:
            raise ValueError("trading_days period needs days >= 1")
        from_date = T
        for _ in range(n - 1):
            from_date = prev_trading_day(from_date, nearest)
    else:
        from_date = nearest(ref, False)
    if from_date > T:
        raise ValueError(f"from={from_date} is after T={T} (period={period})")
    return PeriodWindow(period=period, T=T, ref_date=ref, from_date=from_date,
                        base_date=prev_trading_day(from_date, nearest))


def all_windows(T: str, periods: Mapping[str, Mapping], nearest: NearestBday) -> dict[str, PeriodWindow]:
    """config `periods` 전체 → {period: PeriodWindow}. 순서는 config 순서를 유지."""
    return {name: period_window(name, T, spec, nearest) for name, spec in periods.items()}
