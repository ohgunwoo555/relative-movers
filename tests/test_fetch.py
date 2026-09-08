"""src/fetch.py — 캐시·재시도·sleep·관리종목 소스 동작 테스트 (가짜 pykrx, 네트워크 불필요)."""
from __future__ import annotations

import datetime as dt
import logging
import types

import pandas as pd
import pytest

from src.calendar import resolve_base_date
from src.fetch import (
    KRXCredentialsError, KRXUnavailableError, Fetcher, _from_jsonable, _to_jsonable, install_default_timeout,
)

CONFIG = {"fetch": {"sleep_sec": 1, "max_retries": 3, "cache_dir": "unused"}}
LISTED_ROWS = [  # ISU_SRT_CD, ISU_ABBRV, SECT_TP_NM
    ("005930", "삼성전자", ""), ("005935", "삼성전자우", ""), ("058430", "포스코스틸리온", ""),
    ("060310", "3S", "중견기업부"), ("012345", "관리코스닥", "관리종목(소속부없음)"), ("437780", "삼성스팩8호", "SPAC(소속부없음)"),
]


class FakeStock:
    def __init__(self, fail_times: int = 0):
        self.calls: list[tuple] = []
        self.fail_times = fail_times

    def _maybe_fail(self, name):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError(f"{name} boom")

    def get_nearest_business_day_in_a_week(self, date, prev=True):
        self.calls.append(("nearest", date, prev))
        self._maybe_fail("nearest")
        d = dt.datetime.strptime(date, "%Y%m%d").date()
        step = dt.timedelta(days=-1 if prev else 1)
        while d.weekday() >= 5:
            d += step
        return d.strftime("%Y%m%d")

    def get_etf_ticker_list(self, date):
        self.calls.append(("etf", date)); return ["069500", "069660"]

    def get_etn_ticker_list(self, date):
        self.calls.append(("etn", date)); return ["500001"]

    def get_index_ohlcv(self, f, t, code):
        self.calls.append(("index", f, t, code))
        idx = pd.DatetimeIndex(pd.to_datetime(["20260828", "20260831", "20260907"], format="%Y%m%d"), name="날짜")
        return pd.DataFrame({"시가": [1.0, 2.0, 3.0], "종가": [3000.5, 3010.0, 3050.25], "거래량": [1, 2, 3]}, index=idx)

    def get_market_cap(self, date, market="KOSPI"):
        self.calls.append(("cap", date, market))
        return pd.DataFrame({"종가": [70000], "시가총액": [4.2e14], "거래량": [1], "거래대금": [2], "상장주식수": [3]},
                            index=pd.Index(["005930"], name="티커"))

    def get_market_price_change(self, f, t, market="KOSPI", adjusted=True):
        self.calls.append(("price_change", f, t, market, adjusted))
        self._maybe_fail("price_change")
        return pd.DataFrame({"종목명": ["삼성전자", "폐지"], "시가": [70000, 100], "종가": [71000, 0], "변동폭": [1000, -100],
                             "등락률": [1.43, -100.0], "거래량": [10, 0], "거래대금": [1, 0]},
                            index=pd.Index(["005930", "999990"], name="티커"))

    def get_market_ohlcv(self, f, t, ticker, adjusted=True):
        self.calls.append(("ohlcv", f, t, ticker, adjusted))
        idx = pd.DatetimeIndex(pd.to_datetime(["20260904", "20260907"], format="%Y%m%d"), name="날짜")
        return pd.DataFrame({"종가": [4500.0, 4510.0]}, index=idx)


class Fake전종목시세:
    calls: list = []

    def fetch(self, trdDd, mktId):
        Fake전종목시세.calls.append((trdDd, mktId))
        rows = [r for r in LISTED_ROWS if (mktId == "STK") == (r[2] == "")]
        return pd.DataFrame(rows, columns=["ISU_SRT_CD", "ISU_ABBRV", "SECT_TP_NM"])


def make_fetcher(tmp_path, *, fail_times=0, http_post=None, sleep_log=None):
    stock = FakeStock(fail_times)
    ns = types.SimpleNamespace(stock=stock, core=types.SimpleNamespace(전종목시세=Fake전종목시세))
    sleeps = sleep_log if sleep_log is not None else []
    f = Fetcher(CONFIG, cache_dir=tmp_path / "cache", pykrx_ns=ns, sleep_fn=sleeps.append,
                http_post=http_post, env={"KRX_ID": "x", "KRX_PW": "y"})
    return f, stock, sleeps


def html_with_codes(codes):
    return "<table>" + "".join(f"<tr><td><a onclick=\"companysummary_open('{c}')\">x</a></td></tr>" for c in codes) + "</table>"


def fake_post_factory(text, status=200):
    def post(url, data=None, headers=None, timeout=None):
        fake_post_factory.last = {"url": url, "data": data, "timeout": timeout}
        return types.SimpleNamespace(status_code=status, text=text)
    return post


# ── 자격증명 / import ────────────────────────────────────────────────────
def test_missing_credentials_raises_before_import(tmp_path):
    f = Fetcher(CONFIG, cache_dir=tmp_path, env={})
    with pytest.raises(KRXCredentialsError):
        f.nearest_business_day("20260908")


def test_timeout_floor_is_15s(tmp_path):
    f = Fetcher({"fetch": {"timeout_sec": 5}}, cache_dir=tmp_path, env={})
    assert f.timeout_sec == 15.0
    assert Fetcher(CONFIG, cache_dir=tmp_path, env={}).timeout_sec == 20.0


def test_install_default_timeout_injects_and_is_idempotent(monkeypatch):
    import requests
    seen = {}

    def recorder(self, method, url, **kw):
        seen.update(kw); return "ok"
    monkeypatch.setattr(requests.Session, "request", recorder)      # 래핑되지 않은 상태로 시작
    install_default_timeout(20)
    wrapped = requests.Session.request
    install_default_timeout(30)                                     # 두 번째 호출은 무시
    assert requests.Session.request is wrapped
    requests.Session().request("GET", "http://example")
    assert seen["timeout"] == 20
    requests.Session().request("GET", "http://example", timeout=3)  # 명시 timeout 은 존중
    assert seen["timeout"] == 3


# ── 캐시 / sleep ─────────────────────────────────────────────────────────
def test_cache_hit_skips_network_and_sleep(tmp_path):
    f, stock, sleeps = make_fetcher(tmp_path)
    assert f.nearest_business_day("20260906") == "20260904"
    assert f.nearest_business_day("20260906") == "20260904"
    assert len(stock.calls) == 1 and f.stats == {"network_calls": 1, "cache_hits": 1, "retries": 0}
    assert sleeps == [1.0]
    assert (tmp_path / "cache" / "20260906" / "nearest_bday__1.json").exists()


def test_cache_survives_new_fetcher_instance(tmp_path):
    f1, stock1, _ = make_fetcher(tmp_path)
    df1 = f1.price_change("20260831", "20260907", "KOSPI")
    f2, stock2, _ = make_fetcher(tmp_path)
    df2 = f2.price_change("20260831", "20260907", "KOSPI")
    assert stock2.calls == [] and f2.stats["cache_hits"] == 1
    pd.testing.assert_frame_equal(df1, df2, check_dtype=False)
    assert list(df2.index) == ["005930", "999990"]              # 티커 문자열 보존 (int 변환 금지)
    assert (tmp_path / "cache" / "20260907" / "price_change__20260831_KOSPI_1.json").exists()


def test_datetime_index_roundtrip(tmp_path):
    f1, _, _ = make_fetcher(tmp_path)
    a = f1.index_ohlcv("20260828", "20260907", "KOSPI")
    f2, stock2, _ = make_fetcher(tmp_path)
    b = f2.index_ohlcv("20260828", "20260907", "1001")          # 시장명/코드 모두 같은 캐시 키
    assert stock2.calls == []
    assert isinstance(b.index, pd.DatetimeIndex) and list(b.index.strftime("%Y%m%d")) == ["20260828", "20260831", "20260907"]
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_jsonable_roundtrip_types():
    assert _from_jsonable(_to_jsonable(["005930", "000660"])) == ["005930", "000660"]
    assert _from_jsonable(_to_jsonable("20260907")) == "20260907"
    s = pd.Series({"005930": "삼성전자"}, name="name")
    r = _from_jsonable(_to_jsonable(s))
    assert r.to_dict() == {"005930": "삼성전자"}
    with pytest.raises(TypeError):
        _to_jsonable(object())


def test_clear_cache(tmp_path):
    f, _, _ = make_fetcher(tmp_path)
    f.etf_tickers("20260907"); f.etn_tickers("20260907"); f.etf_tickers("20260904")
    assert f.clear_cache("20260907") == 2
    assert f.clear_cache() == 1


# ── 재시도 ───────────────────────────────────────────────────────────────
def test_retry_with_backoff_then_success(tmp_path, caplog):
    f, stock, sleeps = make_fetcher(tmp_path, fail_times=2)
    with caplog.at_level(logging.WARNING, logger="src.fetch"):
        assert f.nearest_business_day("20260907") == "20260907"
    assert [c[0] for c in stock.calls] == ["nearest"] * 3
    assert sleeps == [1.0, 2.0, 1.0]                            # 백오프 1s, 2s 후 성공 sleep 1s
    assert f.stats == {"network_calls": 1, "cache_hits": 0, "retries": 2}
    assert sum("attempt" in r.message for r in caplog.records) == 2


def test_retry_exhausted_raises_and_does_not_cache(tmp_path):
    f, stock, sleeps = make_fetcher(tmp_path, fail_times=10)
    with pytest.raises(KRXUnavailableError, match="3회 실패"):
        f.price_change("20260831", "20260907", "KOSDAQ")
    assert len(stock.calls) == 3 and sleeps == [1.0, 2.0]
    assert not list((tmp_path / "cache").rglob("*.json"))


# ── calendar 연동 ────────────────────────────────────────────────────────
def test_nearest_callable_matches_calendar_contract(tmp_path):
    f, _, _ = make_fetcher(tmp_path)
    assert resolve_base_date("20260908", f.nearest) == "20260907"
    assert f.nearest("20260905", False) == "20260907"


# ── 유니버스 입력 ────────────────────────────────────────────────────────
def test_listed_frame_from_전종목시세(tmp_path):
    f, _, _ = make_fetcher(tmp_path)
    kospi = f.listed("20260907", "KOSPI")
    assert list(kospi.columns) == ["name", "sect"] and list(kospi.index) == ["005930", "005935", "058430"]
    kosdaq = f.listed("20260907", "KOSDAQ")
    assert kosdaq.loc["012345", "sect"] == "관리종목(소속부없음)"
    assert Fake전종목시세.calls[-2:] == [("20260907", "STK"), ("20260907", "KSQ")]


def test_administrative_from_kind_when_available(tmp_path):
    post = fake_post_factory(html_with_codes(["058430", "012345", "999999"]))
    f, _, _ = make_fetcher(tmp_path, http_post=post)
    assert f.administrative_tickers("20260907", "KOSPI") == {"058430"}
    assert f.administrative_tickers("20260907", "KOSDAQ") == {"012345"}
    assert fake_post_factory.last["data"]["method"] == "searchAdminIssueSub"
    assert fake_post_factory.last["timeout"] == 20.0
    assert f.stats["cache_hits"] >= 1                             # KIND 응답도 캐시된다


def test_administrative_falls_back_to_sect_for_kosdaq_and_none_for_kospi(tmp_path, caplog):
    post = fake_post_factory("<html>shell page without codes</html>")
    f, _, _ = make_fetcher(tmp_path, http_post=post)
    with caplog.at_level(logging.WARNING, logger="src.fetch"):
        assert f.administrative_tickers("20260907", "KOSDAQ") == {"012345"}
        assert f.administrative_tickers("20260907", "KOSPI") is None
    msgs = [r.message for r in caplog.records]
    assert any("종목코드를 찾지 못함" in m for m in msgs) and any("[KOSPI] 관리종목 소스 없음" in m for m in msgs)


def test_administrative_none_when_kind_http_error(tmp_path, caplog):
    post = fake_post_factory("", status=500)
    f, _, sleeps = make_fetcher(tmp_path, http_post=post)
    with caplog.at_level(logging.WARNING, logger="src.fetch"):
        assert f.administrative_tickers("20260907", "KOSPI") is None
    assert f.stats["retries"] == 3 and any("KIND 관리종목 조회 실패" in r.message for r in caplog.records)


def test_naver_fallback_kept_but_callable(tmp_path):
    f, stock, _ = make_fetcher(tmp_path)
    df = f.ohlcv_adjusted_naver("20260904", "20260907", "058430")
    assert stock.calls[-1] == ("ohlcv", "20260904", "20260907", "058430", True) and len(df) == 2
