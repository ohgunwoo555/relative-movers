"""src/calendar.py — DESIGN.md 2절 기간 정의 테스트 (가짜 달력, 네트워크 불필요)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from src.calendar import (
    PeriodWindow, all_windows, is_trading_day, is_weekend, period_window, prev_trading_day,
    reference_date, resolve_base_date, subtract_calendar, subtract_months, to_iso,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PERIODS = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["periods"]

# 가짜 휴장일: 주말 + 아래 날짜 (2026 추석 연휴 9/24~9/25, 개천절 대체 10/5, 임의 2026-03-09 월)
HOLIDAYS = {"20260924", "20260925", "20261005", "20260309", "20251001", "20251003", "20251009"}


def fake_nearest(date: str, prev: bool = True) -> str:
    d = dt.datetime.strptime(date, "%Y%m%d").date()
    step = dt.timedelta(days=-1 if prev else 1)
    while d.weekday() >= 5 or d.strftime("%Y%m%d") in HOLIDAYS:
        d += step
    return d.strftime("%Y%m%d")


# ── 날짜 유틸 ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("d, months, expected", [
    (dt.date(2026, 3, 31), 1, dt.date(2026, 2, 28)),   # 말일 보정
    (dt.date(2026, 3, 31), 6, dt.date(2025, 9, 30)),
    (dt.date(2026, 9, 7), 1, dt.date(2026, 8, 7)),
    (dt.date(2026, 9, 7), 6, dt.date(2026, 3, 7)),
    (dt.date(2026, 1, 15), 1, dt.date(2025, 12, 15)),  # 연도 넘김
    (dt.date(2028, 2, 29), 12, dt.date(2027, 2, 28)),  # 윤일
    (dt.date(2026, 5, 31), 3, dt.date(2026, 2, 28)),
])
def test_subtract_months(d, months, expected):
    assert subtract_months(d, months) == expected


def test_subtract_calendar_mixed():
    assert subtract_calendar(dt.date(2026, 9, 7), days=7) == dt.date(2026, 8, 31)
    assert subtract_calendar(dt.date(2026, 9, 7), years=1) == dt.date(2025, 9, 7)
    assert subtract_calendar(dt.date(2026, 9, 7), months=1, days=1) == dt.date(2026, 8, 6)


def test_to_iso():
    assert to_iso("20260907") == "2026-09-07"
    assert to_iso("2026-09-07") == "2026-09-07"


# ── T ───────────────────────────────────────────────────────────────────
def test_is_trading_day_and_prev():
    assert is_trading_day("20260907", fake_nearest)            # 월
    assert not is_trading_day("20260906", fake_nearest)        # 일
    assert not is_trading_day("20260924", fake_nearest)        # 휴장일
    assert prev_trading_day("20260907", fake_nearest) == "20260904"   # 월 → 금
    assert prev_trading_day("20260928", fake_nearest) == "20260923"   # 월 → 연휴(24,25) 건너뛰고 수


def test_resolve_base_date_matches_stage1_run():
    # 1단계 실측: 2026-09-08(화) 실행 → T = 2026-09-07
    assert resolve_base_date("20260908", fake_nearest) == "20260907"


def test_resolve_base_date_monday_gives_friday():
    assert resolve_base_date("20260907", fake_nearest) == "20260904"


@pytest.mark.parametrize("run_date", ["20260905", "20260906"])
def test_resolve_base_date_returns_none_on_weekend(run_date):
    assert is_weekend(run_date) and resolve_base_date(run_date, fake_nearest) is None


@pytest.mark.parametrize("run_date, expected", [
    ("20260924", "20260923"),   # 추석 연휴 첫날(목) → 직전 거래일 수
    ("20260925", "20260923"),   # 연휴 둘째 날 → 같은 T (main 이 daily/<T>.csv 로 중복 산출을 막는다)
    ("20260928", "20260923"),   # 연휴 다음 월요일 07:00 → 아직 23일이 직전 거래일
    ("20261005", "20261002"),   # 평일 휴장일(월) → 직전 금요일
])
def test_resolve_base_date_weekday_holiday_gives_previous_trading_day(run_date, expected):
    assert not is_weekend(run_date)
    assert resolve_base_date(run_date, fake_nearest) == expected


def test_resolve_base_date_does_not_query_run_date_at_7am():
    """2026-09-09 07:00 KST 재현: 장 시작 전엔 pykrx nearest(실행일) 이 전날을 돌려준다. 그래도 T 는 실행일 직전 거래일이어야 한다."""
    asked = []

    def nearest_at_7am(date, prev=True):
        asked.append((date, prev))
        if date == "20260909" and prev:
            return "20260908"           # 실행일 행이 아직 없음 → 지수 마지막 행 = 전날
        return fake_nearest(date, prev)
    assert resolve_base_date("20260909", nearest_at_7am) == "20260908"
    assert ("20260909", True) not in asked      # 실행일 자체를 묻지 않는다


# ── 기준 날짜 ────────────────────────────────────────────────────────────
def test_reference_dates_from_config_match_stage1_run():
    # 1단계 실측(T=20260907)에서 nearest_bday 에 넘긴 기준 날짜: 1w 20260831 / 1m 20260807 / 6m 20260307 / 1y 20250907
    T = "20260907"
    got = {p: reference_date(T, spec) for p, spec in CONFIG_PERIODS.items()}
    assert got == {"1d": "20260907", "1w": "20260831", "1m": "20260807", "6m": "20260307", "1y": "20250907"}


def test_reference_date_unknown_kind():
    with pytest.raises(ValueError):
        reference_date("20260907", {"kind": "lunar"})


# ── 기간 구간 ────────────────────────────────────────────────────────────
def test_windows_for_stage1_T():
    T = "20260907"
    w = all_windows(T, CONFIG_PERIODS, fake_nearest)
    assert list(w) == ["1d", "1w", "1m", "6m", "1y"]           # config 순서 유지
    # 1d: from = T, 기준가일 = 직전 거래일
    assert w["1d"] == PeriodWindow("1d", T, T, "20260907", "20260904")
    # 1w: 8/31(월, 거래일) → from = 8/31, 기준가일 = 8/28(금)
    assert (w["1w"].from_date, w["1w"].base_date) == ("20260831", "20260828")
    # 1m: 8/7(금, 거래일) → from = 8/7, 기준가일 = 8/6
    assert (w["1m"].from_date, w["1m"].base_date) == ("20260807", "20260806")
    # 6m: 3/7(토) → 3/9(월)은 가짜 휴장일 → from = 3/10(화), 기준가일 = 3/6(금)
    assert (w["6m"].ref_date, w["6m"].from_date, w["6m"].base_date) == ("20260307", "20260310", "20260306")
    # 1y: 2025-09-07(일) → from = 9/8(월), 기준가일 = 9/5(금)
    assert (w["1y"].from_date, w["1y"].base_date) == ("20250908", "20250905")
    for pw in w.values():
        assert pw.base_date < pw.from_date <= pw.T


def test_from_never_after_T_and_base_before_from_across_holidays():
    # T 가 연휴 직후 거래일(9/28 월)이고 1w 기준 날짜(9/21 월)가 거래일인 경우
    w = period_window("1w", "20260928", CONFIG_PERIODS["1w"], fake_nearest)
    assert (w.from_date, w.base_date) == ("20260921", "20260918")
    # 기준 날짜가 연휴 첫날(9/24 목)이면 from 은 연휴 다음 거래일(9/28 월) = T, 기준가일 = 9/23
    w = period_window("1w", "20261001", {"days": 7, "kind": "calendar"}, fake_nearest)
    assert (w.ref_date, w.from_date, w.base_date) == ("20260924", "20260928", "20260923")


def test_trading_days_kind_steps_back_by_trading_days():
    # days=3 → from = T 에서 2 거래일 전
    w = period_window("3d", "20260928", {"days": 3, "kind": "trading_days"}, fake_nearest)
    assert (w.from_date, w.base_date) == ("20260922", "20260921")   # 9/28 → 9/23 → 9/22, 기준가일 9/21
    with pytest.raises(ValueError):
        period_window("0d", "20260928", {"days": 0, "kind": "trading_days"}, fake_nearest)


def test_period_window_rejects_non_trading_T():
    with pytest.raises(ValueError):
        period_window("1d", "20260906", CONFIG_PERIODS["1d"], fake_nearest)


def test_as_iso():
    w = period_window("1d", "20260907", CONFIG_PERIODS["1d"], fake_nearest)
    assert w.as_iso() == {"period": "1d", "base_date": "2026-09-07",
                          "start_date": "2026-09-07", "base_price_date": "2026-09-04"}


def test_today_kst_uses_seoul_date():
    from src.calendar import KST, today_kst
    now = dt.datetime.now(KST)
    assert today_kst() == now.strftime("%Y%m%d")
    # 22:00 UTC = 다음날 07:00 KST — UTC 날짜와 다르다
    utc_2200 = dt.datetime(2026, 9, 8, 22, 30, tzinfo=dt.timezone.utc)
    assert utc_2200.astimezone(KST).strftime("%Y%m%d") == "20260909" and utc_2200.strftime("%Y%m%d") == "20260908"
