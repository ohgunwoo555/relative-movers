"""src/rank.py — 상위/하위 N, 동률 규칙, long-format 컬럼."""
from __future__ import annotations

import pandas as pd
import pytest

from src.calc import RESULT_COLUMNS
from src.rank import rank_movers, to_markdown_table, top_and_bottom


def calc_frame(rows):
    df = pd.DataFrame(rows, columns=["ticker", "name", "stock_ret", "market_cap"])
    df["market_ret"] = 2.0
    df["excess_ret"] = df["stock_ret"] - 2.0
    df["base_date"], df["market"], df["period"], df["start_date"] = "2026-09-07", "KOSPI", "1d", "2026-09-07"
    df["start_close"], df["end_close"], df["trading_value"] = 100.0, 100.0 + df["stock_ret"], 1e9
    return df


ROWS = [("A", "a", 10.0, 1e12), ("B", "b", -5.0, 5e11), ("C", "c", 2.0, 2e12), ("D", "d", 7.0, 3e11),
        ("E", "e", 7.0, 9e11), ("F", "f", 7.0, 9e11), ("G", "g", -9.0, 1e11)]


def test_rank_up_and_down_with_ties():
    df = calc_frame(ROWS)
    up = rank_movers(df, 4, "up")
    # excess 8(A) > 5(D,E,F) : 동률은 시총 큰 순(E,F 9e11 > D 3e11), 같으면 ticker 오름차순(E < F)
    assert list(up["ticker"]) == ["A", "E", "F", "D"] and list(up["rank"]) == [1, 2, 3, 4]
    assert (up["direction"] == "up").all()
    down = rank_movers(df, 2, "down")
    assert list(down["ticker"]) == ["G", "B"] and list(down["excess_ret"]) == [-11.0, -7.0]


def test_top_and_bottom_columns_and_shape():
    out = top_and_bottom(calc_frame(ROWS), 3)
    assert list(out.columns) == RESULT_COLUMNS
    assert len(out) == 6 and list(out["direction"]) == ["up"] * 3 + ["down"] * 3
    assert list(out["rank"]) == [1, 2, 3, 1, 2, 3]


def test_top_n_larger_than_frame():
    out = rank_movers(calc_frame(ROWS[:2]), 20, "up")
    assert len(out) == 2 and list(out["rank"]) == [1, 2]


def test_nan_excess_dropped_and_nan_cap_sorted_last():
    df = calc_frame(ROWS[:3])
    df.loc[df["ticker"] == "C", "excess_ret"] = float("nan")
    df.loc[df["ticker"] == "A", "market_cap"] = float("nan")
    df.loc[df["ticker"] == "B", ["excess_ret", "stock_ret"]] = [8.0, 10.0]   # A 와 excess·stock_ret 동률, A 는 시총 NaN → B 먼저
    up = rank_movers(df, 5, "up")
    assert list(up["ticker"]) == ["B", "A"]


def test_invalid_args():
    with pytest.raises(ValueError):
        rank_movers(calc_frame(ROWS), 0, "up")
    with pytest.raises(ValueError):
        rank_movers(calc_frame(ROWS), 1, "sideways")


def test_markdown_table():
    md = to_markdown_table(top_and_bottom(calc_frame(ROWS), 2), "down")
    assert md.splitlines()[0].startswith("| rank | ticker |")
    assert "| 1 | G | g |" in md and "-11.00%p" in md and "| 2 | B |" in md
