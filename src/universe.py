"""유니버스 + 제외 필터 — DESIGN.md 3절.

기본 유니버스: T 시점 KOSPI / KOSDAQ 상장 종목 전체.

| 제외 대상 | 판별 방법 (이 모듈) | config 키 |
|---|---|---|
| ETF | pykrx `get_etf_ticker_list(T)` 목록 (fetch.py 가 넘겨줌) | 항상 제외 |
| ETN | pykrx `get_etn_ticker_list(T)` 목록 | `exclude.etn` |
| 스팩 | 종목명에 "스팩" 포함 | `exclude.spac` |
| 우선주 | 종목코드 끝자리 != '0' (우선주·기타 종류주·신주인수권증권 등 비보통주 전부) **또는** 종목명의 강한 접미사(N우, 우B, 우C, 우(전환), 우(신형)) | `exclude.preferred` |
| 관리종목 | 호출자가 넘기는 티커 집합. 없으면 **경고 로그 후 미적용** (조용히 넘어가지 않음) | `exclude.administrative` |
| 거래정지 | 기간 중 거래량 0 — `suspended_mask` 로 calc 단계에서 적용 (여기서는 인터페이스만) | `exclude.suspended` |
| 신규상장 | 기간 수익률 프레임과 T 유니버스의 inner join 으로 자동 탈락 | 항상 제외 |
| 상장폐지 | pykrx 가 `종가 0, 등락률 -100` 행으로 덧붙임 → `inner_join_universe` 로 제거 | (자연 탈락) |

이 모듈은 네트워크를 모른다. 입력은 fetch.py 가 만들어 넘기는 DataFrame/집합이다.
`listed` 프레임 규약: index = 티커(str), 컬럼 `name`(종목명), 선택 `sect`(KRX 전종목시세의 소속부 SECT_TP_NM).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import pandas as pd

log = logging.getLogger(__name__)

# 종목명 접미사 규칙 — '강한' 접미사만 본다: 현대차2우B / CJ4우(전환) / 한화3우B / 두산우C / 우(신형).
# 단순 '…우' 는 성우·에코글로우·이오플로우 같은 보통주를 오탐하므로(2026-09-08 validate_universe 실측) 제외한다.
# 단순 '…우' 우선주(삼성전자우 005935 등)는 코드 끝자리 규칙이 전부 잡는다(실측: official_not_ours 없음).
PREFERRED_NAME_RE = re.compile(r"(?:\d우B?|우[BC]|우\((?:전환|신형)\))$")
SPAC_KEYWORD = "스팩"
ADMINISTRATIVE_SECT_KEYWORD = "관리종목"

PRICE_CHANGE_CLOSE_COL = "종가"
PRICE_CHANGE_RET_COL = "등락률"
PRICE_CHANGE_VOLUME_COL = "거래량"


# ──────────────────────────────────────────────────────────────────────────
# 입력 프레임 구성
# ──────────────────────────────────────────────────────────────────────────
def listed_frame(ticker_to_name: Mapping[str, str] | pd.Series,
                 sect: Mapping[str, str] | pd.Series | None = None) -> pd.DataFrame:
    """{티커: 종목명}(+ 선택 {티커: 소속부}) → `listed` 규약 프레임."""
    names = pd.Series(dict(ticker_to_name), dtype="object", name="name")
    df = names.to_frame()
    df.index = df.index.astype(str)
    df.index.name = "ticker"
    df["name"] = df["name"].astype(str).str.strip()
    if sect is not None:
        s = pd.Series(dict(sect), dtype="object")
        s.index = s.index.astype(str)
        df["sect"] = s.reindex(df.index).fillna("").astype(str).str.strip()
    return df


# ──────────────────────────────────────────────────────────────────────────
# 개별 판별 규칙 (순수 함수)
# ──────────────────────────────────────────────────────────────────────────
def is_preferred_ticker(ticker: str) -> bool:
    """KRX 종목코드 끝자리: '0' = 보통주, 그 외(5/7/9/K/L …) = 우선주·기타 종류주·신주인수권증권(WL) 등 비보통주.

    실측(2026-09-08): 공식 주식종류 '우선주' 는 전부 이 규칙에 포함되고, 추가로 K/WL 접미 코드 13종(비보통주)이 걸린다.
    유니버스는 보통주만 대상으로 하므로 이들을 함께 제외하는 것이 의도에 맞는다.
    """
    return len(ticker) > 0 and ticker[-1] != "0"


def is_preferred_name(name: str) -> bool:
    return bool(PREFERRED_NAME_RE.search(name.strip()))


def is_spac_name(name: str) -> bool:
    return SPAC_KEYWORD in name


def preferred_mask(listed: pd.DataFrame) -> pd.Series:
    by_ticker = pd.Series([is_preferred_ticker(t) for t in listed.index], index=listed.index)
    by_name = listed["name"].map(is_preferred_name)
    return (by_ticker | by_name).rename("preferred")


def preferred_rule_breakdown(listed: pd.DataFrame) -> pd.DataFrame:
    """검증용: 티커 규칙과 이름 규칙이 어긋나는 종목 (한쪽만 걸린 것)."""
    by_ticker = pd.Series([is_preferred_ticker(t) for t in listed.index], index=listed.index, name="by_ticker")
    by_name = listed["name"].map(is_preferred_name).rename("by_name")
    df = pd.concat([listed["name"], by_ticker, by_name], axis=1)
    return df[df["by_ticker"] != df["by_name"]]


def spac_mask(listed: pd.DataFrame) -> pd.Series:
    return listed["name"].map(is_spac_name).rename("spac")


def etx_mask(listed: pd.DataFrame, tickers: Iterable[str]) -> pd.Series:
    s = set(str(t) for t in tickers)
    return pd.Series([t in s for t in listed.index], index=listed.index, name="etx")


def administrative_from_sect(listed: pd.DataFrame, col: str = "sect") -> set[str] | None:
    """KRX 전종목시세의 소속부(SECT_TP_NM)에 '관리종목' 이 표기된 티커.

    KOSDAQ 은 소속부로 관리종목이 표기되지만 KOSPI 커버리지는 검증 전이다(docs/administrative_issue.md).
    컬럼이 없으면 None (= 판별 불가).
    """
    if col not in listed.columns:
        return None
    return set(listed.index[listed[col].astype(str).str.contains(ADMINISTRATIVE_SECT_KEYWORD, na=False)])


def administrative_mask(listed: pd.DataFrame, tickers: Iterable[str]) -> pd.Series:
    s = set(str(t) for t in tickers)
    return pd.Series([t in s for t in listed.index], index=listed.index, name="administrative")


# ──────────────────────────────────────────────────────────────────────────
# 유니버스 구성
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class UniverseResult:
    market: str
    df: pd.DataFrame                                   # 최종 유니버스 (listed 규약)
    steps: list[tuple[str, int]] = field(default_factory=list)   # [(단계, 남은 종목 수)]
    removed: dict[str, list[str]] = field(default_factory=dict)  # {단계: 제거된 티커}
    warnings: list[str] = field(default_factory=list)

    @property
    def tickers(self) -> list[str]:
        return list(self.df.index)

    def summary(self) -> str:
        parts = [f"{name}={n}" for name, n in self.steps]
        return f"[{self.market}] " + " → ".join(parts)


def build_universe(listed: pd.DataFrame, market: str, config: Mapping, *,
                   etf_tickers: Iterable[str],
                   etn_tickers: Iterable[str] | None = None,
                   administrative_tickers: Iterable[str] | None = None) -> UniverseResult:
    """DESIGN.md 3절 제외 규칙을 config 순서대로 적용한다.

    Args:
        listed: T 시점 상장 종목 (`listed_frame` 규약)
        market: "KOSPI" | "KOSDAQ" (로그·결과 표시용)
        config: config.yaml 전체 (exclude.* 사용)
        etf_tickers: T 시점 ETF 티커 (항상 제외)
        etn_tickers: T 시점 ETN 티커. `exclude.etn` 이 켜져 있는데 None 이면 경고 후 미적용
        administrative_tickers: T 시점 관리종목 티커. `exclude.administrative` 가 켜져 있는데 None 이면 경고 후 미적용
    """
    exclude = dict(config.get("exclude", {}) or {})
    df = listed.copy()
    if "name" not in df.columns:
        raise ValueError("listed frame needs a 'name' column")
    res = UniverseResult(market=market, df=df)
    res.steps.append(("listed", len(df)))

    def drop(step: str, mask: pd.Series):
        nonlocal df
        removed = list(df.index[mask.reindex(df.index).fillna(False).astype(bool)])
        df = df.drop(index=removed)
        res.removed[step] = removed
        res.steps.append((step, len(df)))

    def unsupported(step: str, key: str, why: str):
        msg = f"[{market}] exclude.{key}=true 이지만 {why} — {step} 필터 미적용"
        log.warning(msg)
        res.warnings.append(msg)
        res.steps.append((f"{step}(미적용)", len(df)))

    # ETF: 항상 제외
    drop("-etf", etx_mask(df, etf_tickers))

    if exclude.get("etn", True):
        if etn_tickers is None:
            unsupported("-etn", "etn", "ETN 목록이 제공되지 않음")
        else:
            drop("-etn", etx_mask(df, etn_tickers))

    if exclude.get("spac", True):
        drop("-spac", spac_mask(df))

    if exclude.get("preferred", True):
        drop("-preferred", preferred_mask(df))

    if exclude.get("administrative", True):
        if administrative_tickers is None:
            unsupported("-administrative", "administrative",
                        "관리종목 목록이 제공되지 않음(판별 소스 미확정, docs/administrative_issue.md)")
        else:
            drop("-administrative", administrative_mask(df, administrative_tickers))

    res.df = df
    log.info(res.summary())
    return res


# ──────────────────────────────────────────────────────────────────────────
# 기간 수익률 프레임과의 결합 (calc 단계에서 사용)
# ──────────────────────────────────────────────────────────────────────────
def inner_join_universe(price_change: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """`get_market_price_change(from, T)` 결과를 T 시점 유니버스와 inner join.

    - 구간 중 상장폐지 종목(pykrx 가 `종가 0, 등락률 -100` 으로 덧붙임)은 T 유니버스에 없으므로 탈락
    - 신규상장(시작일 데이터 없음) 종목은 price_change 에 없으므로 탈락 (DESIGN.md 3절 '항상 제외')
    - 안전장치로 종가 0 행은 남아 있더라도 제거한다
    유니버스의 `name` 은 price_change 의 `종목명` 과 별개로 `name` 컬럼에 붙인다.
    """
    pc = price_change.copy()
    pc.index = pc.index.astype(str)
    joined = pc.join(universe[["name"]], how="inner")
    if PRICE_CHANGE_CLOSE_COL in joined.columns:
        joined = joined[joined[PRICE_CHANGE_CLOSE_COL] != 0]
    return joined


def suspended_mask(price_change: pd.DataFrame, volume_col: str = PRICE_CHANGE_VOLUME_COL) -> pd.Series:
    """거래정지 판별 인터페이스 (DESIGN.md 3절: 기간 중 거래량 0).

    `get_market_price_change(from, T)` 의 `거래량` 은 구간 누적 거래량이므로 0 이면 구간 내내 거래가 없었다는 뜻이다.
    calc 단계에서 `exclude.suspended` 가 켜져 있을 때 이 마스크로 제외한다.
    """
    return (price_change[volume_col].fillna(0) == 0).rename("suspended")


def apply_suspended(price_change: pd.DataFrame, config: Mapping) -> pd.DataFrame:
    """`exclude.suspended` 가 켜져 있으면 거래량 0 행 제거. 꺼져 있으면 그대로."""
    if (config.get("exclude", {}) or {}).get("suspended", True):
        return price_change[~suspended_mask(price_change)]
    return price_change
