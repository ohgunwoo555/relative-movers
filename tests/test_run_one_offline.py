"""scripts/run_one.py 를 가짜 pykrx 로 끝까지 실행 — calendar → fetch → universe → calc → rank 연결 검증 (네트워크 불필요)."""
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
SCRIPT = ROOT / "scripts" / "run_one.py"
SUMMARIZE = ROOT / "scripts" / "summarize_result.py"

T, T_PREV = "20260907", "20260904"
# (티커, 종목명, 소속부, 기준가, 종가(T), 거래량, 거래대금, 시가총액)
KOSPI_ROWS = [
    ("005930", "삼성전자", "", 70000, 72100, 10_000_000, 7e11, 4.3e14),      # +3.0%
    ("000660", "SK하이닉스", "", 200000, 190000, 3_000_000, 6e11, 1.4e14),   # -5.0%
    ("005380", "현대차", "", 250000, 250000, 1_000_000, 2.5e11, 5e13),       # 0%
    ("058430", "포스코스틸리온", "", 4500, 4950, 500_000, 2e9, 2.7e11),       # +10%
    ("005935", "삼성전자우", "", 60000, 66000, 1_000_000, 6e10, 5e13),        # 우선주 → 제외
    ("437780", "삼성스팩8호", "SPAC", 2000, 2100, 100, 2e5, 1e10),            # 스팩 → 제외
    ("111110", "정지종목", "", 1000, 1000, 0, 0, 1e10),                      # 거래정지 → 제외
]
DELISTED_ROW = ("999990", "폐지종목", "", 500, 0, 0, 0, 0)                     # -100 행
NEW_LISTING = ("222220", "신규상장", "")                                        # price_change 에 없음


def bday(date, prev=True):
    d = dt.datetime.strptime(date, "%Y%m%d").date()
    step = dt.timedelta(days=-1 if prev else 1)
    while d.weekday() >= 5:
        d += step
    return d.strftime("%Y%m%d")


class FakeStock:
    get_nearest_business_day_in_a_week = staticmethod(bday)

    @staticmethod
    def get_etf_ticker_list(date): return ["069500"]

    @staticmethod
    def get_etn_ticker_list(date): return ["500001"]

    @staticmethod
    def get_index_ohlcv(f, t, code):
        # f~t 의 거래일(주말 제외) 일봉. 첫날 3000 → 마지막날 3030 (+1%), 중간은 선형
        days = []
        d = dt.datetime.strptime(f, "%Y%m%d").date()
        end = dt.datetime.strptime(t, "%Y%m%d").date()
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d += dt.timedelta(days=1)
        n = len(days)
        closes = [3000.0 + 30.0 * i / (n - 1) for i in range(n)] if n > 1 else [3000.0]
        idx = pd.DatetimeIndex(pd.to_datetime(days), name="날짜")
        return pd.DataFrame({"시가": 1, "고가": 1, "저가": 1, "종가": closes, "거래량": 1}, index=idx)

    @staticmethod
    def get_market_cap(date, market="KOSPI"):
        return pd.DataFrame({"종가": [r[4] for r in KOSPI_ROWS], "시가총액": [r[7] for r in KOSPI_ROWS], "거래량": 1, "거래대금": 1,
                             "상장주식수": 1}, index=pd.Index([r[0] for r in KOSPI_ROWS], name="티커"))

    @staticmethod
    def get_market_price_change(f, t, market="KOSPI", adjusted=True):
        rows = KOSPI_ROWS + [DELISTED_ROW]
        df = pd.DataFrame({"종목명": [r[1] for r in rows], "시가": [r[3] for r in rows], "종가": [r[4] for r in rows],
                           "변동폭": [r[4] - r[3] for r in rows],
                           "등락률": [round((r[4] / r[3] - 1) * 100, 2) if r[4] else -100.0 for r in rows],
                           "거래량": [r[5] for r in rows], "거래대금": [r[6] for r in rows]},
                          index=pd.Index([r[0] for r in rows], name="티커"))
        return df


class Fake전종목시세:
    def fetch(self, trdDd, mktId):
        rows = [(r[0], r[1], r[2]) for r in KOSPI_ROWS] + [NEW_LISTING]
        return pd.DataFrame(rows, columns=["ISU_SRT_CD", "ISU_ABBRV", "SECT_TP_NM"])


def install_fake_pykrx(monkeypatch):
    core = types.ModuleType("pykrx.website.krx.market.core"); core.전종목시세 = Fake전종목시세
    market = types.ModuleType("pykrx.website.krx.market"); market.core = core
    krx = types.ModuleType("pykrx.website.krx"); krx.market = market
    website = types.ModuleType("pykrx.website"); website.krx = krx
    pk = types.ModuleType("pykrx"); pk.stock = FakeStock(); pk.website = website; pk.__version__ = "fake"
    for name, mod in [("pykrx", pk), ("pykrx.website", website), ("pykrx.website.krx", krx),
                      ("pykrx.website.krx.market", market), ("pykrx.website.krx.market.core", core)]:
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setitem(sys.modules, "pykrx.stock", pk.stock)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run(tmp_path, monkeypatch):
    install_fake_pykrx(monkeypatch)
    monkeypatch.setenv("KRX_ID", "x"); monkeypatch.setenv("KRX_PW", "y")
    import src.fetch as fetch_mod
    monkeypatch.setattr(fetch_mod, "install_default_timeout", lambda *_: None)   # 전역 requests 패치 방지
    monkeypatch.setattr(fetch_mod.Fetcher, "_kind_administrative_codes", lambda self, T: None)  # KIND 네트워크 차단
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda *_: None)
    mod = load(SCRIPT, "run_one_under_test")
    mod.ROOT = tmp_path                                       # outputs/ 를 임시 경로로
    mod.RESULTS_DIR = tmp_path / "results"
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    def _run(*argv):
        monkeypatch.setattr(sys, "argv", ["run_one", "--config", str(tmp_path / "config.yaml"),
                                          "--cache-dir", str(tmp_path / "cache"), *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main()
        return rc, buf.getvalue(), json.loads((tmp_path / "results" / "calc_result.json").read_text(encoding="utf-8"))
    return _run


def test_kospi_1d_end_to_end(run, tmp_path):
    rc, out, r = run("--market", "KOSPI", "--period", "1d", "--base-date", "20260908", "--top-n", "3")
    assert rc == 0 and r["T"] == T
    assert r["window"] == {"ref_date": T, "from": T, "base_date": T_PREV}
    # 유니버스: 8 상장(신규 포함) → 스팩 -1 → 우선주 -1 → 관리종목 미적용(KOSPI 소속부 없음, KIND 없음)
    assert r["universe"]["steps"][0] == ["listed", 8]
    assert r["universe"]["steps"][-1][0] == "-administrative(미적용)"
    assert any("administrative" in w for w in r["universe"]["warnings"])
    # 계산: 폐지(-100)·신규상장·거래정지·우선주·스팩 제거 → 4종목
    assert r["inputs"]["n_delisted_rows"] == 1 and r["calc"]["n_rows"] == 4
    assert r["calc"]["market_ret"] == pytest.approx(1.0)
    ranks = pd.DataFrame(r["rankings"])
    up = ranks[ranks["direction"] == "up"].sort_values("rank")
    down = ranks[ranks["direction"] == "down"].sort_values("rank")
    assert list(up["ticker"]) == ["058430", "005930", "005380"]          # +9, +2, -1
    assert list(down["ticker"]) == ["000660", "005380", "005930"]        # -6, -1, +2
    assert up.iloc[0]["excess_ret"] == pytest.approx(9.0) and down.iloc[0]["excess_ret"] == pytest.approx(-6.0)
    assert set(ranks["ticker"]).isdisjoint({"999990", "222220", "111110", "005935", "437780"})
    assert list(ranks.columns) == ["base_date", "market", "period", "direction", "rank", "ticker", "name", "start_date",
                                   "start_close", "end_close", "stock_ret", "market_ret", "excess_ret", "market_cap", "trading_value"]
    assert "### up 3" in out and "| 1 | 058430 | 포스코스틸리온 |" in out
    assert (tmp_path / "outputs" / T / "movers_KOSPI_1d.csv").exists()
    assert (tmp_path / "results" / f"calc_result_{T}_KOSPI_1d.json").exists()
    # summarize 가 calc JSON 을 읽는다
    sr = load(SUMMARIZE, "summarize_under_test"); sr.RESULTS = tmp_path / "results"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert sr.main("calc") == 0
    assert "**KOSPI 1d**" in buf.getvalue() and "| 1 | 000660 |" in buf.getvalue()


def test_holiday_exits_with_code_3(run):
    rc, out, r = run("--market", "KOSPI", "--period", "1d", "--base-date", "20260906")
    assert rc == 3 and "휴장일" in r["note"]


def test_second_run_uses_cache(run):
    run("--market", "KOSPI", "--period", "1w", "--base-date", "20260908")
    rc, _, r = run("--market", "KOSPI", "--period", "1w", "--base-date", "20260908")
    assert rc == 0 and r["fetch_stats"]["network_calls"] == 0 and r["fetch_stats"]["cache_hits"] >= 8
