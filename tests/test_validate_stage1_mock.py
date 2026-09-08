"""scripts/validate_stage1.py 판정 로직 오프라인 테스트.

가짜 pykrx 로 10:1 액면분할(포스코스틸리온, 2026-04-23) 시나리오를 만들어
- DESIGN.md 2절 기간 정의 실측(definition_check)
- 수정주가 반영 판정(ADJUSTED / NOT_ADJUSTED)
- 종목코드 ↔ 종목명 교차확인(대체 / TICKER_MISMATCH)
이 기대대로 동작하는지 확인한다. 네트워크·KRX 계정 불필요.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import contextlib
import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "validate_stage1.py"
SPLIT = dt.date(2026, 4, 23)
NAMES = {"058430": "포스코스틸리온", "005930": "삼성전자", "000660": "SK하이닉스"}


class FakeKRX:
    """주말만 휴장인 가짜 거래소. adjusted_ok=False 면 price_change(adjusted=True)가 원시가로 동작(미반영 시뮬레이션)."""

    def __init__(self, adjusted_ok: bool = True):
        self.adjusted_ok = adjusted_ok

    @staticmethod
    def _d(s: str) -> dt.date:
        return dt.datetime.strptime(s, "%Y%m%d").date()

    @staticmethod
    def bday(date: str, prev: bool = True) -> str:
        d = FakeKRX._d(date)
        step = -1 if prev else 1
        while d.weekday() >= 5:
            d += dt.timedelta(days=step)
        return d.strftime("%Y%m%d")

    @staticmethod
    def bdays(f: str, t: str):
        d, end = FakeKRX._d(f), FakeKRX._d(t)
        while d <= end:
            if d.weekday() < 5:
                yield d
            d += dt.timedelta(days=1)

    @staticmethod
    def raw_close(tk: str, d: dt.date) -> float:
        if tk == "058430":
            base = 45000 + (d.toordinal() % 7) * 100
            return base if d < SPLIT else base / 10
        if tk == "005930":
            return 70000 + (d.toordinal() - dt.date(2026, 1, 1).toordinal()) * 10
        return 100000.0

    @classmethod
    def adj_close(cls, tk: str, d: dt.date) -> float:
        c = cls.raw_close(tk, d)
        return c / 10 if (tk == "058430" and d < SPLIT) else c

    def ohlcv(self, f, t, tk, adjusted=True):
        fn = self.adj_close if adjusted else self.raw_close
        idx = [pd.Timestamp(d) for d in self.bdays(f, t)]
        return pd.DataFrame({"종가": [fn(tk, i.date()) for i in idx]}, index=pd.DatetimeIndex(idx, name="날짜"))

    def price_change(self, f, t, market="KOSPI", adjusted=True):
        f, t = self.bday(f, prev=False), self.bday(t)
        fprev = self._d(self.bday((self._d(f) - dt.timedelta(days=1)).strftime("%Y%m%d")))
        fn = self.adj_close if (adjusted and self.adjusted_ok) else self.raw_close
        rows = []
        for tk, nm in NAMES.items():
            b, c = fn(tk, fprev), fn(tk, self._d(t))
            rows.append({"티커": tk, "종목명": nm, "시가": b, "종가": c, "변동폭": c - b,
                         "등락률": round((c / b - 1) * 100, 2), "거래량": 1, "거래대금": 1})
        return pd.DataFrame(rows).set_index("티커")

    def index_ohlcv(self, f, t, code):
        idx = [pd.Timestamp(d) for d in self.bdays(f, t)]
        return pd.DataFrame({"종가": [3000 + i for i in range(len(idx))]}, index=pd.DatetimeIndex(idx))

    def module(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            get_nearest_business_day_in_a_week=self.bday,
            get_market_ticker_list=lambda date, market="KOSPI": list(NAMES) if market == "KOSPI" else ["900250"],
            get_etf_ticker_list=lambda date: ["069500"],
            get_index_ohlcv=self.index_ohlcv,
            get_market_cap=lambda date, market="KOSPI": pd.DataFrame(
                {"종가": [1], "시가총액": [1], "거래량": [1], "거래대금": [1], "상장주식수": [1]}),
            get_market_price_change=self.price_change,
            get_market_ohlcv=self.ohlcv,
            get_market_ticker_name=lambda tk: NAMES.get(tk, ""),
        )


def run_script(tmp_path, monkeypatch, fake: FakeKRX, extra_args: list[str]) -> tuple[int, dict]:
    stock = fake.module()
    pk = types.ModuleType("pykrx")
    pk.__version__ = "mock"
    pk.stock = stock
    monkeypatch.setitem(sys.modules, "pykrx", pk)
    monkeypatch.setitem(sys.modules, "pykrx.stock", stock)
    monkeypatch.setenv("KRX_ID", "x")
    monkeypatch.setenv("KRX_PW", "y")

    spec = importlib.util.spec_from_file_location("validate_stage1_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SLEEP_SEC = 0
    mod.RESULT_JSON = tmp_path / "stage1_result.json"
    monkeypatch.setattr(sys, "argv", ["validate_stage1", "--base-date", "20260908", "--skip-1y", *extra_args])
    with contextlib.redirect_stdout(io.StringIO()):
        rc = mod.main()
    return rc, json.loads(mod.RESULT_JSON.read_text(encoding="utf-8"))


def test_definition_and_adjusted(tmp_path, monkeypatch):
    rc, r = run_script(tmp_path, monkeypatch, FakeKRX(adjusted_ok=True), [])
    assert rc == 0
    assert r["T"] == "20260907"                      # 2026-09-08(화) 실행 → 직전 거래일 월요일
    assert r["periods"]["1d"]["from"] == r["T"]      # 1d 는 from = T
    assert r["periods"]["1w"]["ref_date"] == "20260831"  # T-7일 (월요일)
    assert r["periods"]["1w"]["from"] == "20260831"      # 기준 날짜가 거래일이면 from = 기준 날짜
    d = r["definition_check"]
    assert d["verdict"] == "OK"
    assert d["base_price_matches_prev_close"] and d["close_matches_T"] and d["ret_matches_formula"]
    assert d["pc_1w"]["base_price_matches_prev_close"] is True
    s = r["split_check"]
    assert s["verdict"] == "ADJUSTED"
    assert s["name_mismatch"] is False
    assert s["diff_A_vs_C_pp"] < 1.0
    assert s["raw_vs_adjusted_gap_pp"] > 80          # 10:1 분할이 구간 안에 있음
    assert s["base_price_matches_adjusted_prev_close"] is True
    assert r["index_1w"]["1001"]["ret_pct"] is not None


def test_not_adjusted(tmp_path, monkeypatch):
    _, r = run_script(tmp_path, monkeypatch, FakeKRX(adjusted_ok=False), [])
    s = r["split_check"]
    assert s["verdict"] == "NOT_ADJUSTED"
    assert s["price_change_adjusted"]["등락률"] == pytest.approx(-90, abs=1)


def test_ticker_resolved_by_name(tmp_path, monkeypatch):
    _, r = run_script(tmp_path, monkeypatch, FakeKRX(), ["--split-ticker", "000660", "--split-name", "포스코스틸리온"])
    s = r["split_check"]
    assert s["name_mismatch"] is True
    assert s["ticker_resolved_by_name"] == "058430"
    assert s["ticker"] == "058430"
    assert s["verdict"] == "ADJUSTED"


def test_ticker_mismatch_aborts(tmp_path, monkeypatch):
    _, r = run_script(tmp_path, monkeypatch, FakeKRX(), ["--split-ticker", "000660", "--split-name", "없는회사"])
    assert r["split_check"]["verdict"] == "TICKER_MISMATCH"
    assert "window" not in r["split_check"]
