"""결과 출력 — CSV / Markdown / SQLite (DESIGN.md 6절 5단계·7절).

- `write_csv`      outputs/<T>/movers.csv          (long format, DESIGN.md 7절 컬럼 순서)
- `write_markdown` outputs/<T>/movers.md           (시장·기간별 상위/하위 표 + 시장 수익률 요약 + 경고/실패)
- `upsert_sqlite`  data/movers.db 테이블 `movers`  (PK = base_date, market, period, direction, rank → INSERT OR REPLACE)
이 모듈은 계산을 하지 않는다. 입력은 rank.py 가 만든 long-format 프레임이다.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from src.calc import RESULT_COLUMNS
from src.rank import to_markdown_table

SQLITE_TABLE = "movers"
SQLITE_DDL = f"""
CREATE TABLE IF NOT EXISTS {SQLITE_TABLE} (
    base_date     TEXT    NOT NULL,
    market        TEXT    NOT NULL,
    period        TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    rank          INTEGER NOT NULL,
    ticker        TEXT    NOT NULL,
    name          TEXT,
    start_date    TEXT,
    start_close   REAL,
    end_close     REAL,
    stock_ret     REAL,
    market_ret    REAL,
    excess_ret    REAL,
    market_cap    REAL,
    trading_value REAL,
    updated_at    TEXT,
    PRIMARY KEY (base_date, market, period, direction, rank)
)
"""


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in RESULT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"movers frame missing columns: {missing}")
    return df[RESULT_COLUMNS]


def write_csv(movers: pd.DataFrame, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_columns(movers).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _fmt_pct(x) -> str:
    return "-" if x is None or pd.isna(x) else f"{x:+.2f}%"


def render_markdown(movers: pd.DataFrame, *, T: str, combos: Iterable[Mapping], warnings: Iterable[str] = (),
                    failures: Iterable[Mapping] = (), generated_at: str | None = None, top_n: int | None = None) -> str:
    """movers.md 본문.

    combos: [{market, period, from, base_date, market_ret, n_calc_rows, ok, error}] — 실행 순서대로
    """
    combos = list(combos)
    lines = [f"# relative-movers — 기준일 {T}", ""]
    lines.append(f"- 생성: {generated_at or dt.datetime.now().isoformat(timespec='seconds')} · 산출 개수: 상위/하위 {top_n or '-'}")
    lines.append("- 정의: 구간 [from, T], 기준가 = from 직전 거래일 종가, 수익률 = close(T)/기준가 − 1, 초과수익률 = 종목 − 시장 (DESIGN.md 2절)")
    lines += ["", "## 시장 수익률 요약", "",
              "| market | period | from | 기준가일 | market_ret | 종목 수 | 상태 |", "|---|---|---|---|---|---|---|"]
    for c in combos:
        status = "ok" if c.get("ok") else f"FAILED: {str(c.get('error', ''))[:80]}"
        lines.append(f"| {c['market']} | {c['period']} | {c.get('from', '-')} | {c.get('base_date', '-')} | "
                     f"{_fmt_pct(c.get('market_ret'))} | {c.get('n_calc_rows', '-')} | {status} |")
    for c in combos:
        if not c.get("ok"):
            continue
        sub = movers[(movers["market"] == c["market"]) & (movers["period"] == c["period"])]
        lines += ["", f"## {c['market']} {c['period']} — from {c.get('from')}, 기준가일 {c.get('base_date')}, "
                      f"market_ret {_fmt_pct(c.get('market_ret'))}, 종목 {c.get('n_calc_rows')}", ""]
        for direction, title in (("up", "상위 (시장 대비 초과 상승)"), ("down", "하위 (시장 대비 초과 하락)")):
            lines += [f"### {title}", "", to_markdown_table(sub, direction), ""]
    warnings = list(warnings)
    failures = list(failures)
    if warnings or failures:
        lines += ["## 경고 / 실패", ""]
        for w in warnings:
            lines.append(f"- ⚠️ {w}")
        for f in failures:
            lines.append(f"- ❌ {f.get('market')} {f.get('period')}: [{f.get('error_class', 'error')}] {f.get('error')}")
    return "\n".join(lines) + "\n"


def write_markdown(text: str, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def upsert_sqlite(movers: pd.DataFrame, db_path: str | Path) -> int:
    """`movers` 테이블에 INSERT OR REPLACE. 반환: 기록한 행 수."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    df = _ensure_columns(movers).copy()
    df["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    df = df.astype(object).where(pd.notna(df), None)
    cols = RESULT_COLUMNS + ["updated_at"]
    sql = f"INSERT OR REPLACE INTO {SQLITE_TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    rows = [tuple(_py(v) for v in r) for r in df[cols].itertuples(index=False, name=None)]
    with sqlite3.connect(db_path) as con:
        con.execute(SQLITE_DDL)
        con.executemany(sql, rows)
        con.commit()
    return len(rows)


def _py(v):
    """numpy 스칼라 → 파이썬 스칼라 (sqlite3 바인딩용)."""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:  # noqa: BLE001
            return v
    return v


def read_sqlite(db_path: str | Path, base_date: str | None = None) -> pd.DataFrame:
    with sqlite3.connect(db_path) as con:
        if base_date:
            return pd.read_sql_query(f"SELECT * FROM {SQLITE_TABLE} WHERE base_date = ? ORDER BY market, period, direction, rank",
                                     con, params=(base_date,))
        return pd.read_sql_query(f"SELECT * FROM {SQLITE_TABLE} ORDER BY base_date, market, period, direction, rank", con)
