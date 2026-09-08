"""src/report.py — CSV / Markdown / SQLite upsert."""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from src.calc import RESULT_COLUMNS
from src.report import read_sqlite, render_markdown, upsert_sqlite, write_csv, write_markdown


def movers_frame():
    rows = []
    for market, period in (("KOSPI", "1d"), ("KOSDAQ", "1w")):
        for direction in ("up", "down"):
            for rank in (1, 2):
                sign = 1 if direction == "up" else -1
                rows.append({"base_date": "2026-09-07", "market": market, "period": period, "direction": direction, "rank": rank,
                             "ticker": f"{rank:06d}", "name": f"{market}{direction}{rank}", "start_date": "2026-09-07",
                             "start_close": 100.0, "end_close": 100.0 + sign * 10 / rank, "stock_ret": sign * 10.0 / rank,
                             "market_ret": 1.0, "excess_ret": sign * 10.0 / rank - 1.0, "market_cap": 1e12 if rank == 1 else float("nan"),
                             "trading_value": 1e9})
    return pd.DataFrame(rows)[RESULT_COLUMNS]


def test_write_csv_roundtrip(tmp_path):
    p = write_csv(movers_frame(), tmp_path / "out" / "movers.csv")
    back = pd.read_csv(p, dtype={"ticker": str}, encoding="utf-8-sig")
    assert list(back.columns) == RESULT_COLUMNS and len(back) == 8 and back["ticker"].iloc[0] == "000001"


def test_write_csv_requires_columns(tmp_path):
    with pytest.raises(ValueError, match="missing columns"):
        write_csv(movers_frame().drop(columns=["excess_ret"]), tmp_path / "x.csv")


def test_render_markdown_sections(tmp_path):
    combos = [{"market": "KOSPI", "period": "1d", "from": "20260907", "base_date": "20260904", "market_ret": 1.0, "n_calc_rows": 800, "ok": True},
              {"market": "KOSDAQ", "period": "1w", "from": "20260831", "base_date": "20260828", "market_ret": -2.0, "n_calc_rows": 1500, "ok": True},
              {"market": "KOSDAQ", "period": "1y", "ok": False, "error": "CalcError: 지수 종가 누락"}]
    md = render_markdown(movers_frame(), T="20260907", combos=combos, warnings=["[KOSPI] 관리종목 미적용"],
                         failures=[{"market": "KOSDAQ", "period": "1y", "error_class": "CalcError", "error": "지수 종가 누락"}], top_n=2)
    assert md.startswith("# relative-movers — 기준일 20260907")
    assert "| KOSPI | 1d | 20260907 | 20260904 | +1.00% | 800 | ok |" in md
    assert "| KOSDAQ | 1y | - | - | - | - | FAILED: CalcError: 지수 종가 누락 |" in md
    assert "## KOSPI 1d — from 20260907, 기준가일 20260904, market_ret +1.00%, 종목 800" in md
    assert md.count("### 상위") == 2 and md.count("### 하위") == 2       # 성공 조합만 표 출력
    assert "| 1 | 000001 | KOSPIup1 |" in md and "| 1 | 000001 | KOSDAQdown1 |" in md
    assert "- ⚠️ [KOSPI] 관리종목 미적용" in md and "- ❌ KOSDAQ 1y: [CalcError] 지수 종가 누락" in md
    p = write_markdown(md, tmp_path / "o" / "movers.md")
    assert p.read_text(encoding="utf-8") == md


def test_upsert_sqlite_replaces_on_pk(tmp_path):
    db = tmp_path / "data" / "movers.db"
    df = movers_frame()
    assert upsert_sqlite(df, db) == 8
    # 같은 PK 로 값이 바뀐 행을 다시 넣으면 교체된다 (중복 없음)
    df2 = df.copy(); df2.loc[0, "name"] = "바뀐이름"; df2.loc[0, "excess_ret"] = 99.0
    assert upsert_sqlite(df2, db) == 8
    with sqlite3.connect(db) as con:
        n, = con.execute("SELECT COUNT(*) FROM movers").fetchone()
        pk = con.execute("PRAGMA table_info(movers)").fetchall()
    assert n == 8
    assert [c[1] for c in pk if c[5]] == ["base_date", "market", "period", "direction", "rank"]   # PK 컬럼 순서
    back = read_sqlite(db, base_date="2026-09-07")
    row = back[(back["market"] == "KOSPI") & (back["period"] == "1d") & (back["direction"] == "up") & (back["rank"] == 1)].iloc[0]
    assert row["name"] == "바뀐이름" and row["excess_ret"] == 99.0 and row["ticker"] == "000001"
    assert pd.isna(back["market_cap"]).sum() == 4 and "updated_at" in back.columns
    # 다른 기준일은 누적된다
    df3 = df.copy(); df3["base_date"] = "2026-09-08"
    upsert_sqlite(df3, db)
    assert len(read_sqlite(db)) == 16
