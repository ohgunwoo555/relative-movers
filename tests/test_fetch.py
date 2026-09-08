"""src/fetch.py — 캐시·재시도·sleep·관리종목 소스 동작 테스트 (가짜 pykrx, 네트워크 불필요)."""
from __future__ import annotations

import datetime as dt
import logging
import sys
import types

import pandas as pd
import pytest

from src.calendar import resolve_base_date
from src.fetch import (
    KRXCredentialsError, KRXUnavailableError, Fetcher, _from_jsonable, _to_jsonable, classify_krx_failure,
    diagnose_krx_failure, install_default_timeout, purge_pykrx_modules,
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


# ── 실패 진단 / 분류 ─────────────────────────────────────────────────────
import json as _json


def json_err(doc: str):
    return _json.JSONDecodeError("Expecting value", doc, 0)


def fake_get_factory(status=200, text=""):
    def get(url, headers=None, timeout=None):
        fake_get_factory.last = {"url": url, "timeout": timeout}
        return types.SimpleNamespace(status_code=status, text=text)
    return get


@pytest.mark.parametrize("exc, status, body, expected", [
    (ConnectionError("HTTPSConnectionPool: Max retries exceeded (ProxyError 403)"), None, "", "차단"),
    (json_err("<html>x</html>"), 403, "<html>Access Denied</html>", "차단"),
    (json_err("<html>x</html>"), 200, "<html>Request Rejected</html>", "차단"),
    (json_err("<html>x</html>"), 503, "", "점검"),
    (json_err("<html>x</html>"), 200, "<html>시스템 점검 중입니다. 서비스 이용에 불편을 드려 죄송합니다</html>", "점검"),
    (json_err("<html>x</html>"), 200, "<html><title>KRX 로그인</title><form action='login.jsp'>", "자격증명"),
    (RuntimeError("KRX 로그인 실패: 세션 미인증 (자격 증명을 확인하세요)"), 200, "", "자격증명"),
    (KRXCredentialsError("no env"), None, "", "자격증명"),
    (json_err("<html>x</html>"), 200, "<html>hello</html>", "unknown"),
    (RuntimeError("점검 중"), None, "", "점검"),
])
def test_classify_krx_failure(exc, status, body, expected):
    assert classify_krx_failure(exc, status, body) == expected


def test_diagnose_uses_exception_doc_and_probe_status():
    exc = json_err("<html>  시스템   점검 안내  </html>")
    d = diagnose_krx_failure(exc, http_get=fake_get_factory(503, "<html>maintenance</html>"), timeout=20)
    assert d["classification"] == "점검" and d["http_status"] == 503
    assert d["body_head"] == "<html> 시스템 점검 안내 </html>" and d["body_source"] == "exception.doc"
    assert fake_get_factory.last["timeout"] == 20 and d["probe_error"] is None


def test_diagnose_falls_back_to_probe_body_and_records_probe_error():
    d = diagnose_krx_failure(RuntimeError("boom"), http_get=fake_get_factory(200, "<html>Access Denied</html>"))
    assert d["classification"] == "차단" and d["body_source"] == "probe:login_page"

    def bad_get(url, headers=None, timeout=None):
        raise ConnectionError("proxy 403")
    d = diagnose_krx_failure(RuntimeError("boom"), http_get=bad_get)
    assert d["probe_error"].startswith("ConnectionError") and d["http_status"] is None
    assert d["classification"] == "unknown"                    # 예외 자체엔 단서가 없음
    d = diagnose_krx_failure(RuntimeError("boom"), probe=False)
    assert d["http_status"] is None and d["body_source"] is None


def make_import_fetcher(tmp_path, monkeypatch, outcomes, http_get, sleep_log):
    """outcomes: 각 _import_pykrx 호출의 결과 (예외 인스턴스면 raise, 아니면 반환)."""
    monkeypatch.setattr("src.fetch.install_default_timeout", lambda *_: None)
    f = Fetcher({"fetch": {"sleep_sec": 1, "max_retries": 3, "import_retry_backoff_sec": 10}},
                cache_dir=tmp_path, sleep_fn=sleep_log.append, http_get=http_get, env={"KRX_ID": "x", "KRX_PW": "y"})
    calls = []

    def fake_import():
        calls.append(1)
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out
    f._import_pykrx = fake_import
    return f, calls


def test_import_retries_with_backoff_then_succeeds(tmp_path, monkeypatch, caplog):
    ns = types.SimpleNamespace(stock=FakeStock(), core=None)
    sleeps: list = []
    f, calls = make_import_fetcher(tmp_path, monkeypatch, [json_err("<html>점검</html>"), json_err("<html>점검</html>"), ns],
                                   fake_get_factory(503, ""), sleeps)
    with caplog.at_level(logging.WARNING, logger="src.fetch"):
        assert f._pykrx() is ns
    assert len(calls) == 3 and sleeps == [10.0, 20.0]           # import 백오프 10s × attempt
    assert f.stats["retries"] == 2 and f.stats["network_calls"] == 0   # import 는 데이터 호출 통계에 넣지 않는다
    assert sum("[점검]" in r.message and "attempt" in r.message for r in caplog.records) == 2
    assert f._pykrx() is ns and len(calls) == 3                 # 이후 호출은 캐시된 네임스페이스


def test_import_failure_raises_with_classification(tmp_path, monkeypatch):
    sleeps: list = []
    exc = json_err("<html><title>KRX Data Marketplace</title> 서비스 점검 중입니다 </html>")
    f, calls = make_import_fetcher(tmp_path, monkeypatch, [exc], fake_get_factory(200, ""), sleeps)
    with pytest.raises(KRXUnavailableError) as ei:
        f.nearest_business_day("20260907")
    e = ei.value
    assert len(calls) == 3 and sleeps == [10.0, 20.0]
    assert e.classification == "점검" and "[점검]" in str(e) and "3회 실패" in str(e)
    assert e.diagnosis["http_status"] == 200 and "서비스 점검 중입니다" in e.diagnosis["body_head"]
    assert "JSONDecodeError" in e.diagnosis["exception"]


def test_import_blocked_classification_from_network_exception(tmp_path, monkeypatch):
    sleeps: list = []
    exc = ConnectionError("HTTPSConnectionPool(host='data.krx.co.kr'): Max retries exceeded (ProxyError 403)")
    f, _ = make_import_fetcher(tmp_path, monkeypatch, [exc], fake_get_factory(200, ""), sleeps)
    with pytest.raises(KRXUnavailableError) as ei:
        f.etf_tickers("20260907")
    assert ei.value.classification == "차단"


def test_check_authenticated_rejects_unauthenticated_session():
    auth_none = types.SimpleNamespace(get_auth_session=lambda: None)
    with pytest.raises(RuntimeError, match="세션 미인증"):
        Fetcher._check_authenticated(auth_none)
    auth_bad = types.SimpleNamespace(get_auth_session=lambda: types.SimpleNamespace(is_authenticated=False))
    with pytest.raises(RuntimeError):
        Fetcher._check_authenticated(auth_bad)
    auth_ok = types.SimpleNamespace(get_auth_session=lambda: types.SimpleNamespace(is_authenticated=True))
    Fetcher._check_authenticated(auth_ok)


def test_data_call_json_error_is_classified(tmp_path):
    stock = FakeStock()

    def html_response(date, prev=True):
        raise json_err("<html><form action='/contents/MDC/COMS/client/login.jsp'>로그인</form></html>")
    stock.get_nearest_business_day_in_a_week = html_response
    ns = types.SimpleNamespace(stock=stock, core=None)
    f = Fetcher(CONFIG, cache_dir=tmp_path, pykrx_ns=ns, sleep_fn=lambda *_: None,
                http_get=fake_get_factory(200, ""), env={"KRX_ID": "x", "KRX_PW": "y"})
    with pytest.raises(KRXUnavailableError) as ei:
        f.nearest_business_day("20260907")
    assert ei.value.classification == "자격증명" and "login.jsp" in ei.value.diagnosis["body_head"]


def test_purge_pykrx_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "pykrx", types.ModuleType("pykrx"))
    monkeypatch.setitem(sys.modules, "pykrx.website.comm.webio", types.ModuleType("pykrx.website.comm.webio"))
    monkeypatch.setitem(sys.modules, "pykrxlike", types.ModuleType("pykrxlike"))
    assert purge_pykrx_modules() == 2
    assert "pykrx" not in sys.modules and "pykrxlike" in sys.modules
