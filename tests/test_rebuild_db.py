"""scripts/rebuild_db.py — daily CSV → SQLite 재구성."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from src.calc import RESULT_COLUMNS
from src.report import read_sqlite

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("rebuild_db", ROOT / "scripts" / "rebuild_db.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def daily_csv(path: Path, base_date: str, n: int = 3, name_suffix=""):
    rows = [{"base_date": base_date, "market": "KOSPI", "period": "1d", "direction": "up", "rank": i,
             "ticker": f"{i:06d}", "name": f"n{i}{name_suffix}", "start_date": base_date, "start_close": 1.0, "end_close": 2.0,
             "stock_ret": 1.0, "market_ret": 0.5, "excess_ret": 0.5, "market_cap": 1.0, "trading_value": 1.0} for i in range(1, n + 1)]
    pd.DataFrame(rows)[RESULT_COLUMNS].to_csv(path, index=False, encoding="utf-8-sig")


def test_rebuild_from_daily_csvs(tmp_path):
    m = load()
    daily = tmp_path / "daily"; daily.mkdir()
    daily_csv(daily / "20260904.csv", "2026-09-04", 3)
    daily_csv(daily / "20260907.csv", "2026-09-07", 4)
    db = tmp_path / "data" / "movers.db"
    info = m.rebuild(daily, db)
    assert info["files"] == 2 and info["rows_in_db"] == 7 and info["per_base_date"] == {"2026-09-04": 3, "2026-09-07": 4}
    assert info["bad_files"] == []
    # 다시 실행해도 같은 결과 (기본: 새로 만든다)
    assert m.rebuild(daily, db)["rows_in_db"] == 7
    # 같은 T 파일을 갱신하면 마지막 값이 이긴다
    daily_csv(daily / "20260907.csv", "2026-09-07", 4, name_suffix="new")
    assert read_sqlite(m.rebuild(daily, db)["db"], "2026-09-07")["name"].str.endswith("new").all()


def test_rebuild_skips_bad_files_and_keep_mode(tmp_path):
    m = load()
    daily = tmp_path / "daily"; daily.mkdir()
    daily_csv(daily / "20260907.csv", "2026-09-07", 2)
    (daily / "broken.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    db = tmp_path / "movers.db"
    info = m.rebuild(daily, db)
    assert info["rows_in_db"] == 2 and info["bad_files"] and "broken.csv" in info["bad_files"][0]
    # --keep: 기존 DB 에 upsert (다른 날짜 추가)
    daily_csv(daily / "20260908.csv", "2026-09-08", 1)
    assert m.rebuild(daily, db, keep=True)["rows_in_db"] == 3


def test_rebuild_empty_dir_creates_schema(tmp_path):
    m = load()
    daily = tmp_path / "daily"; daily.mkdir()
    info = m.rebuild(daily, tmp_path / "x.db")
    assert info["rows_in_db"] == 0 and (tmp_path / "x.db").exists()
