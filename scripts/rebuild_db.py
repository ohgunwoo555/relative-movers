#!/usr/bin/env python3
"""docs/results/daily/<T>.csv 전체를 읽어 SQLite(data/movers.db)를 재구성한다.

DB 는 git 에 커밋하지 않는다(설계 변경 2026-09-08). 일별 CSV 가 원본이고 DB 는 파생물이다.

    python scripts/rebuild_db.py [--daily-dir docs/results/daily] [--db data/movers.db] [--keep]

--keep 이 없으면 기존 DB 파일을 지우고 새로 만든다 (기본). --keep 이면 기존 DB 에 upsert 만 한다.
종료코드: 0 성공 / 1 CSV 없음·컬럼 불일치
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.calc import RESULT_COLUMNS  # noqa: E402
from src.report import read_sqlite, upsert_sqlite  # noqa: E402


def load_daily(daily_dir: Path) -> tuple[pd.DataFrame, list[str]]:
    files = sorted(daily_dir.glob("*.csv"))
    frames, bad = [], []
    for f in files:
        df = pd.read_csv(f, dtype={"ticker": str}, encoding="utf-8-sig")
        missing = [c for c in RESULT_COLUMNS if c not in df.columns]
        if missing:
            bad.append(f"{f.name}: missing {missing}")
            continue
        frames.append(df[RESULT_COLUMNS])
    if not frames:
        return pd.DataFrame(columns=RESULT_COLUMNS), bad
    all_df = pd.concat(frames, ignore_index=True)
    # 같은 PK 가 여러 파일에 있으면 마지막 파일(정렬상 최신 T)이 이긴다
    all_df = all_df.drop_duplicates(subset=["base_date", "market", "period", "direction", "rank"], keep="last")
    return all_df, bad


def rebuild(daily_dir: Path, db: Path, keep: bool = False) -> dict:
    df, bad = load_daily(daily_dir)
    if not keep and db.exists():
        db.unlink()
    n = upsert_sqlite(df, db) if len(df) else 0
    if not len(df) and not db.exists():
        # 빈 DB 도 스키마는 만들어 둔다
        upsert_sqlite(pd.DataFrame(columns=RESULT_COLUMNS), db)
    stored = read_sqlite(db)
    per_date = stored.groupby("base_date").size().to_dict() if len(stored) else {}
    return {"files": len(list(daily_dir.glob("*.csv"))), "rows_loaded": int(len(df)), "rows_written": n,
            "rows_in_db": int(len(stored)), "per_base_date": per_date, "bad_files": bad, "db": str(db)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily-dir", default=str(ROOT / "docs" / "results" / "daily"))
    ap.add_argument("--db", default=str(ROOT / "data" / "movers.db"))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    daily = Path(args.daily_dir)
    if not daily.exists():
        print(f"daily dir not found: {daily}", file=sys.stderr)
        return 1
    info = rebuild(daily, Path(args.db), keep=args.keep)
    print(f"rebuilt {info['db']}: files={info['files']} rows={info['rows_in_db']} per_base_date={info['per_base_date']}")
    for b in info["bad_files"]:
        print(f"  skipped {b}", file=sys.stderr)
    return 1 if info["bad_files"] or info["files"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
