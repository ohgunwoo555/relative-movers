"""상위/하위 N 랭킹 — DESIGN.md 2절 (정렬 키 = excess_ret) · 7절 (long format).

동률 처리 규칙:
  - 정렬 키 excess_ret 가 같으면 stock_ret (up: 큰 쪽, down: 작은 쪽) → market_cap 큰 쪽 → ticker 오름차순.
  - rank 는 정렬 후 위치(1..N)이며 공동 순위를 두지 않는다 (SQLite PK (base_date, market, period, direction, rank) 유지).
  - 부동소수 미세 차이로 순서가 흔들리지 않도록 excess_ret/stock_ret 는 소수 6자리에서 반올림해 비교한다.
"""
from __future__ import annotations

import pandas as pd

from src.calc import RESULT_COLUMNS

TIE_DECIMALS = 6


def _sorted(df: pd.DataFrame, direction: str) -> pd.DataFrame:
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")
    asc = direction == "down"
    tmp = df.copy()
    tmp["_x"] = tmp["excess_ret"].round(TIE_DECIMALS)
    tmp["_s"] = tmp["stock_ret"].round(TIE_DECIMALS)
    tmp["_c"] = tmp["market_cap"].fillna(-1.0)
    tmp = tmp.sort_values(["_x", "_s", "_c", "ticker"], ascending=[asc, asc, False, True], kind="mergesort")
    return tmp.drop(columns=["_x", "_s", "_c"])


def rank_movers(df: pd.DataFrame, top_n: int, direction: str) -> pd.DataFrame:
    """excess_ret 기준 direction('up'|'down') 상위 top_n. rank 1..N 과 direction 컬럼을 채운다."""
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    ranked = _sorted(df.dropna(subset=["excess_ret"]), direction).head(top_n).copy()
    ranked["direction"] = direction
    ranked["rank"] = range(1, len(ranked) + 1)
    return ranked[RESULT_COLUMNS].reset_index(drop=True)


def top_and_bottom(df: pd.DataFrame, top_n: int) -> pd.DataFrame:
    """up N + down N 을 하나의 long-format 프레임으로."""
    return pd.concat([rank_movers(df, top_n, "up"), rank_movers(df, top_n, "down")], ignore_index=True)


def to_markdown_table(ranked: pd.DataFrame, direction: str) -> str:
    """Summary/리포트용 마크다운 표 (한 방향)."""
    sub = ranked[ranked["direction"] == direction]
    lines = ["| rank | ticker | name | start_close | end_close | stock_ret | market_ret | excess_ret | market_cap(억) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for _, r in sub.iterrows():
        cap = "" if pd.isna(r["market_cap"]) else f"{r['market_cap'] / 1e8:,.0f}"
        lines.append(f"| {r['rank']} | {r['ticker']} | {r['name']} | {r['start_close']:,.0f} | {r['end_close']:,.0f} | "
                     f"{r['stock_ret']:+.2f}% | {r['market_ret']:+.2f}% | {r['excess_ret']:+.2f}%p | {cap} |")
    return "\n".join(lines)
