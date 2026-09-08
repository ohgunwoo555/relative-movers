"""src/universe.py — DESIGN.md 3절 제외 규칙 테스트 (네트워크 불필요)."""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.universe import (
    administrative_from_sect, apply_suspended, build_universe, inner_join_universe, is_preferred_name,
    is_preferred_ticker, is_spac_name, listed_frame, preferred_mask, preferred_rule_breakdown,
    spac_mask, suspended_mask,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

LISTED = {
    "005930": "삼성전자",
    "005935": "삼성전자우",
    "005380": "현대차",
    "005385": "현대차우",
    "005387": "현대차2우B",
    "005389": "현대차3우B",
    "00104K": "CJ4우(전환)",
    "051910": "LG화학",
    "051915": "LG화학우",
    "000880": "한화",
    "00088K": "한화3우B",
    "377300": "카카오페이",
    "437780": "삼성스팩8호",
    "455910": "NH스팩29호",
    "069500": "KODEX 200",          # ETF (KOSPI 목록에 섞였다고 가정)
    "500001": "신한 인버스 ETN",     # ETN
    "058430": "포스코스틸리온",
    "900250": "크리스탈신소재",       # 외국기업, 코드 0 종료
    "012345": "코드끝자리5보통",       # 규칙 충돌 검증용 가짜: 코드로는 우선주, 이름은 아님
}
SECT = {"005930": "", "437780": "SPAC", "058430": "관리종목", "377300": ""}


@pytest.fixture
def listed() -> pd.DataFrame:
    return listed_frame(LISTED, SECT)


# ── 입력 프레임 ──────────────────────────────────────────────────────────
def test_listed_frame_shape(listed):
    assert list(listed.columns) == ["name", "sect"]
    assert listed.index.name == "ticker"
    assert listed.loc["005930", "name"] == "삼성전자"
    assert listed.loc["051910", "sect"] == ""          # sect 미제공 티커는 빈 문자열


def test_listed_frame_from_series():
    s = pd.Series({"005930": " 삼성전자 "})
    df = listed_frame(s)
    assert df.loc["005930", "name"] == "삼성전자" and "sect" not in df.columns


# ── 우선주 ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("ticker, expected", [
    ("005930", False), ("005935", True), ("005387", True), ("00104K", True), ("00088K", True),
    ("900250", False), ("", False),
])
def test_is_preferred_ticker(ticker, expected):
    assert is_preferred_ticker(ticker) is expected


@pytest.mark.parametrize("name, expected", [
    ("삼성전자우", True), ("현대차2우B", True), ("현대차3우B", True), ("CJ4우(전환)", True),
    ("LG화학우", True), ("한화3우B", True), ("SK케미칼우", True), ("두산우C", True), ("우리금융지주", False),
    ("삼성전자", False), ("대우건설", False), ("현대우", True), ("포스코스틸리온", False), ("삼성스팩8호", False),
])
def test_is_preferred_name(name, expected):
    assert is_preferred_name(name) is expected


def test_preferred_mask_is_union_of_rules(listed):
    m = preferred_mask(listed)
    # ETN 코드(500001)도 끝자리가 0이 아니라 코드 규칙에 걸린다 — 실제 파이프라인에서는 ETN 단계에서 먼저 빠진다
    assert set(listed.index[m]) == {"005935", "005385", "005387", "005389", "00104K", "051915", "00088K", "500001", "012345"}
    bd = preferred_rule_breakdown(listed)
    assert list(bd.index) == ["500001", "012345"]       # 코드 규칙만 걸린 종목이 보고된다
    assert bool(bd.loc["012345", "by_ticker"]) and not bool(bd.loc["012345", "by_name"])


# ── 스팩 ─────────────────────────────────────────────────────────────────
def test_spac(listed):
    assert is_spac_name("하나금융25호스팩") and is_spac_name("대신밸런스제17호스팩")
    assert not is_spac_name("스펙트럼") and not is_spac_name("삼성전자")   # '스팩' 포함 여부만 본다
    assert set(listed.index[spac_mask(listed)]) == {"437780", "455910"}


# ── 관리종목 ─────────────────────────────────────────────────────────────
def test_administrative_from_sect(listed):
    assert administrative_from_sect(listed) == {"058430"}
    assert administrative_from_sect(listed.drop(columns="sect")) is None


# ── build_universe ───────────────────────────────────────────────────────
def test_build_universe_all_filters(listed):
    res = build_universe(listed, "KOSPI", CONFIG, etf_tickers={"069500"}, etn_tickers={"500001"},
                         administrative_tickers={"058430"})
    assert res.tickers == ["005930", "005380", "051910", "000880", "377300", "900250"]
    assert res.steps == [("listed", 19), ("-etf", 18), ("-etn", 17), ("-spac", 15),
                         ("-preferred", 7), ("-administrative", 6)]
    assert res.removed["-preferred"] == ["005935", "005385", "005387", "005389", "00104K", "051915", "00088K", "012345"]
    assert res.warnings == []
    assert res.summary().startswith("[KOSPI] listed=19 → -etf=18")


def test_build_universe_warns_when_administrative_source_missing(listed, caplog):
    with caplog.at_level(logging.WARNING, logger="src.universe"):
        res = build_universe(listed, "KOSDAQ", CONFIG, etf_tickers={"069500"}, etn_tickers={"500001"},
                             administrative_tickers=None)
    assert "058430" in res.tickers                       # 미적용 → 관리종목이 남아 있다
    assert res.steps[-1] == ("-administrative(미적용)", 7)
    assert len(res.warnings) == 1 and "exclude.administrative=true" in res.warnings[0]
    assert any("administrative" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_build_universe_warns_when_etn_list_missing(listed, caplog):
    with caplog.at_level(logging.WARNING, logger="src.universe"):
        res = build_universe(listed, "KOSPI", CONFIG, etf_tickers=set(), etn_tickers=None, administrative_tickers=set())
    assert ("-etn(미적용)", 19) in res.steps
    assert res.removed.get("-etn") is None
    assert "500001" in res.removed["-preferred"]          # ETN 단계가 빠져도 코드 끝자리 규칙에서 걸린다
    assert any("exclude.etn=true" in w for w in res.warnings)


def test_build_universe_respects_config_flags(listed):
    cfg = {"exclude": {"etn": False, "spac": False, "preferred": False, "administrative": False}}
    res = build_universe(listed, "KOSPI", cfg, etf_tickers={"069500"})
    assert res.steps == [("listed", 19), ("-etf", 18)]   # ETF 만 항상 제외
    assert res.warnings == []


def test_build_universe_requires_name_column():
    with pytest.raises(ValueError):
        build_universe(pd.DataFrame(index=["005930"]), "KOSPI", CONFIG, etf_tickers=set())


# ── inner join / 거래정지 ────────────────────────────────────────────────
def make_price_change() -> pd.DataFrame:
    # pykrx get_market_price_change(from, T) 형태. 상장폐지 종목은 종가 0 / 등락률 -100 으로 덧붙는다.
    return pd.DataFrame({
        "종목명": ["삼성전자", "현대차", "삼성전자우", "폐지종목", "정지종목"],
        "시가": [70000, 200000, 60000, 1000, 5000],
        "종가": [71000, 190000, 61000, 0, 5000],
        "변동폭": [1000, -10000, 1000, -1000, 0],
        "등락률": [1.43, -5.0, 1.67, -100.0, 0.0],
        "거래량": [100, 200, 50, 0, 0],
        "거래대금": [1, 1, 1, 0, 0],
    }, index=pd.Index(["005930", "005380", "005935", "999990", "888880"], name="티커"))


def test_inner_join_drops_delisted_and_non_universe(listed):
    universe = build_universe(listed, "KOSPI", CONFIG, etf_tickers=set(), etn_tickers=set(), administrative_tickers=set()).df
    pc = make_price_change()
    joined = inner_join_universe(pc, universe)
    assert list(joined.index) == ["005930", "005380"]    # 우선주·상장폐지(-100)·유니버스 밖 탈락
    assert joined.loc["005930", "name"] == "삼성전자" and "종목명" in joined.columns


def test_inner_join_drops_close_zero_even_if_in_universe():
    universe = listed_frame({"999990": "폐지종목"})
    joined = inner_join_universe(make_price_change(), universe)
    assert joined.empty


def test_inner_join_new_listing_absent_from_price_change(listed):
    # 신규상장 종목(T 유니버스에는 있지만 기간 수익률에는 없음)은 join 으로 자연 탈락
    universe = listed_frame({"005930": "삼성전자", "111110": "신규상장"})
    joined = inner_join_universe(make_price_change(), universe)
    assert list(joined.index) == ["005930"]


def test_suspended_mask_and_apply():
    pc = make_price_change()
    assert list(pc.index[suspended_mask(pc)]) == ["999990", "888880"]
    assert list(apply_suspended(pc, CONFIG).index) == ["005930", "005380", "005935"]
    assert len(apply_suspended(pc, {"exclude": {"suspended": False}})) == 5
