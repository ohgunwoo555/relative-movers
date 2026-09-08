"""src/main.py — 2시장 × 5기간 루프, 통합 출력, 부분 실패 계속 진행, 종료코드 (가짜 pykrx)."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3

import pandas as pd
import pytest

from src import main as m
from src.calc import RESULT_COLUMNS
from src.fetch import Fetcher
from tests import fake_pykrx as fp


@pytest.fixture
def env(tmp_path, monkeypatch):
    stock = fp.install(monkeypatch)
    monkeypatch.setenv("KRX_ID", "x"); monkeypatch.setenv("KRX_PW", "y")
    config = m.load_config()
    config["fetch"]["cache_dir"] = str(tmp_path / "cache")
    config["output"]["dir"] = str(tmp_path / "outputs")
    config["output"]["sqlite_path"] = str(tmp_path / "data" / "movers.db")
    return tmp_path, config, stock


def test_full_run_two_markets_five_periods(env):
    tmp_path, config, stock = env
    fetcher = Fetcher(config)
    res = m.run(config, fetcher, "20260908", top_n=2)
    assert res.T == fp.T and res.exit_code == m.EXIT_OK
    assert [(c.market, c.period) for c in res.combos] == [(mk, p) for mk in ("KOSPI", "KOSDAQ") for p in ("1d", "1w", "1m", "6m", "1y")]
    assert all(c.ok for c in res.combos)
    movers = res.movers
    assert list(movers.columns) == RESULT_COLUMNS
    assert len(movers) == 10 * 2 * 2                          # 10 조합 × (up 2 + down 2)
    assert movers.groupby(["market", "period", "direction"]).size().eq(2).all()
    # 유니버스는 시장당 1회 (listed 2회), ETF/ETN 1회, 시총은 시장당 1회
    assert sum(1 for c in stock.calls if c[0] == "etf") == 1 and sum(1 for c in stock.calls if c[0] == "cap") == 2
    assert res.universes["KOSPI"]["steps"][-1] == ["-administrative(미적용)", 6] or res.universes["KOSPI"]["steps"][-1] == ("-administrative(미적용)", 6)
    assert res.universes["KOSDAQ"]["steps"][-1][1] == 4       # 5 상장 - 관리종목 1
    assert any("KOSPI" in w for w in res.warnings)
    # 시장 수익률 부호: KOSPI +1%, KOSDAQ -2%
    kospi = movers[(movers["market"] == "KOSPI") & (movers["period"] == "1d")]
    kosdaq = movers[(movers["market"] == "KOSDAQ") & (movers["period"] == "1d")]
    assert kospi["market_ret"].iloc[0] == pytest.approx(1.0) and kosdaq["market_ret"].iloc[0] == pytest.approx(-2.0)
    assert kospi[kospi["direction"] == "up"].iloc[0]["ticker"] == "058430"         # +10 - 1 = +9
    assert kosdaq[kosdaq["direction"] == "up"].iloc[0]["ticker"] == "060310"       # +10 + 2 = +12
    assert "012345" not in set(movers["ticker"]) and "999990" not in set(movers["ticker"])
    # 타이밍: 1y price_change 가 기록된다 (4-1절 보완용)
    c1y = next(c for c in res.combos if c.market == "KOSPI" and c.period == "1y")
    assert "price_change" in c1y.timings and c1y.window["base_date"] == "20250905"

    paths = m.write_outputs(res, config)
    out = tmp_path / "outputs" / fp.T
    assert (out / "movers.csv").exists() and (out / "movers.md").exists()
    assert paths["sqlite_rows"] == 40
    with sqlite3.connect(tmp_path / "data" / "movers.db") as con:
        assert con.execute("SELECT COUNT(*) FROM movers").fetchone()[0] == 40
    md = (out / "movers.md").read_text(encoding="utf-8")
    assert md.count("## KOSPI ") == 5 and md.count("## KOSDAQ ") == 5 and "⚠️" in md
    # 두 번째 저장은 교체 (중복 누적 없음)
    assert m.write_outputs(res, config)["sqlite_rows"] == 40
    with sqlite3.connect(tmp_path / "data" / "movers.db") as con:
        assert con.execute("SELECT COUNT(*) FROM movers").fetchone()[0] == 40


def test_partial_failure_continues_and_reports(env, monkeypatch):
    tmp_path, config, stock = env
    orig = stock.get_index_ohlcv

    def flaky_index(f, t, code):
        if code == "2001" and f == "20260828":                # KOSDAQ 1w 지수만 실패
            raise ConnectionError("HTTPSConnectionPool: Max retries exceeded (ProxyError)")
        return orig(f, t, code)
    stock.get_index_ohlcv = flaky_index
    config["fetch"]["max_retries"] = 1
    res = m.run(config, Fetcher(config), "20260908", top_n=1)
    assert res.exit_code == m.EXIT_FAILED
    assert [(c.market, c.period) for c in res.failures] == [("KOSDAQ", "1w")]
    assert res.failures[0].error_class == "krx:차단" and "1회 실패" in res.failures[0].error
    assert sum(c.ok for c in res.combos) == 9 and len(res.movers) == 9 * 2
    paths = m.write_outputs(res, config)
    md = (tmp_path / "outputs" / fp.T / "movers.md").read_text(encoding="utf-8")
    assert "| KOSDAQ | 1w | - | - | - | - | FAILED:" in md or "| KOSDAQ | 1w | 20260831 | 20260828 | - | 0 | FAILED:" in md
    assert "- ❌ KOSDAQ 1w: [krx:차단]" in md and paths["sqlite_rows"] == 18


def test_market_preparation_failure_fails_all_periods_of_that_market(env):
    tmp_path, config, stock = env

    def broken_cap(date, market="KOSPI"):
        if market == "KOSPI":
            raise RuntimeError("cap boom")
        return fp.FakeStock.get_market_cap(stock, date, market)
    stock.get_market_cap = broken_cap
    config["fetch"]["max_retries"] = 1
    res = m.run(config, Fetcher(config), "20260908", top_n=1)
    assert [(c.market, c.period) for c in res.failures] == [("KOSPI", p) for p in ("1d", "1w", "1m", "6m", "1y")]
    assert all(c.ok for c in res.combos if c.market == "KOSDAQ")
    assert res.exit_code == m.EXIT_FAILED


def test_holiday_returns_exit_3_and_writes_nothing(env):
    tmp_path, config, _ = env
    res = m.run(config, Fetcher(config), "20260906")
    assert res.T is None and res.exit_code == m.EXIT_HOLIDAY and "휴장일" in res.note
    assert m.write_outputs(res, config) == {}


def test_krx_unavailable_at_T_gives_failure_classification(env, monkeypatch):
    tmp_path, config, stock = env
    import json as _json

    def html(date, prev=True):
        raise _json.JSONDecodeError("Expecting value", "<html>서비스 점검 중입니다</html>", 0)
    stock.get_nearest_business_day_in_a_week = html
    config["fetch"]["max_retries"] = 1
    f = Fetcher(config, http_get=lambda *a, **k: type("R", (), {"status_code": 503, "text": ""})())
    res = m.run(config, f, "20260908")
    assert res.T is None and res.exit_code == m.EXIT_FAILED
    assert res.failure["classification"] == "점검" and "점검" in res.failure["body_head"]


def test_cli_main_end_to_end(env, monkeypatch):
    tmp_path, config, _ = env
    cfg_path = tmp_path / "config.yaml"
    import yaml
    cfg_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    result_json = tmp_path / "results" / "main_result.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main(["--base-date", "20260908", "--config", str(cfg_path), "--top-n", "2",
                     "--result-json", str(result_json), "--sqlite-path", str(tmp_path / "db" / "m.db")])
    assert rc == 0
    r = json.loads(result_json.read_text(encoding="utf-8"))
    assert r["T"] == fp.T and r["exit_code"] == 0 and r["n_movers_rows"] == 40 and len(r["combos"]) == 10
    assert r["paths"]["sqlite"].endswith("m.db") and r["paths"]["sqlite_rows"] == 40
    assert "10/10 조합 성공" in buf.getvalue()
    # 시장·기간 제한과 --no-db
    with contextlib.redirect_stdout(io.StringIO()):
        rc = m.main(["--base-date", "20260908", "--config", str(cfg_path), "--markets", "KOSDAQ", "--periods", "1d,1w",
                     "--no-db", "--result-json", str(result_json)])
    r = json.loads(result_json.read_text(encoding="utf-8"))
    assert rc == 0 and len(r["combos"]) == 2 and "sqlite" not in r["paths"]
    # 휴장일 → exit 3
    with contextlib.redirect_stdout(io.StringIO()):
        assert m.main(["--base-date", "20260906", "--config", str(cfg_path)]) == 3
