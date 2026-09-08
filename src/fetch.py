"""pykrx 래퍼 — 캐시·재시도·sleep (DESIGN.md 4절).

책임
  - pykrx 지연 import. `KRX_ID`/`KRX_PW` 미설정이면 `KRXCredentialsError`, KRX 접속·로그인 실패면 `KRXUnavailableError`\n    (import 실패도 재시도·백오프 대상. 실패 원인을 점검/차단/자격증명/unknown 으로 분류해 메시지와 `.diagnosis` 에 담는다)
  - 호출 간 `sleep(config.fetch.sleep_sec)`, 실패 시 `config.fetch.max_retries` 회 재시도(백오프), HTTP 타임아웃 기본 20초
    (1단계 실측: 엔드포인트별 첫 호출 6~8초)
  - 응답을 `config.fetch.cache_dir/<날짜>/<엔드포인트>__<인자>.json` 에 저장, 재실행 시 캐시 우선
  - calendar.py 에 넘길 거래일 조회 콜러블(`Fetcher.nearest`)
  - universe.py 에 넘길 입력: `listed(T, market)`(종목명+소속부), ETF/ETN 목록, 관리종목 집합(실패 시 None)
  - 네이버 수정주가 OHLCV 는 미사용 예외 경로로 남겨 둔다 (`ohlcv_adjusted_naver`)

이 모듈은 랭킹·수익률 계산을 하지 않는다 (모듈 경계, CLAUDE.md 5절).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from src.universe import administrative_from_sect, listed_frame

log = logging.getLogger(__name__)

MARKET_ID = {"KOSPI": "STK", "KOSDAQ": "KSQ", "ALL": "ALL"}
INDEX_CODE = {"KOSPI": "1001", "KOSDAQ": "2001"}
KIND_ADMIN_URL = "https://kind.krx.co.kr/investwarn/adminissue.do"
KIND_CODE_RE = re.compile(r"companysummary_open\('([0-9A-Z]{6})'\)")
DEFAULT_TIMEOUT_SEC = 20


KRX_LOGIN_PAGE = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd"
PROBE_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
FAILURE_CLASSES = ("점검", "차단", "자격증명", "unknown")
DEFAULT_IMPORT_BACKOFF_SEC = 10


class KRXCredentialsError(RuntimeError):
    """KRX_ID / KRX_PW 환경변수가 없다 (pykrx >= 1.2 는 KRX Data Marketplace 로그인 필수)."""


class KRXUnavailableError(RuntimeError):
    """KRX 접속·로그인·조회가 재시도 후에도 실패했다. `.diagnosis` 에 분류·HTTP 상태·응답 앞부분이 담긴다."""

    def __init__(self, message: str, diagnosis: dict | None = None):
        super().__init__(message)
        self.diagnosis = diagnosis or {}

    @property
    def classification(self) -> str:
        return self.diagnosis.get("classification", "unknown")


# ──────────────────────────────────────────────────────────────────────────
# 실패 진단: KRX 가 JSON 대신 HTML 을 주거나 접속이 끊겼을 때 "점검 / 차단 / 자격증명 / unknown" 으로 분류
# ──────────────────────────────────────────────────────────────────────────
_NETWORK_EXC = ("proxyerror", "connectionerror", "connecttimeout", "readtimeout", "timeout", "sslerror",
                "max retries exceeded", "connection refused", "connection reset", "remotedisconnected")
_MAINT_KW = ("점검", "maintenance", "서비스 이용에 불편", "일시적으로", "서비스가 원활", "temporarily unavailable", "service unavailable")
_BLOCK_KW = ("access denied", "차단", "blocked", "forbidden", "not allowed", "captcha", "bot detected", "request rejected")
_CRED_KW = ("자격 증명", "credential", "비밀번호", "아이디 또는", "로그인 실패", "login failed", "세션 미인증",
            "mdccoms001", "login.jsp", "로그인", "login")


def body_head(text: str | None, n: int = 300) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()[:n]


def classify_krx_failure(exc: BaseException | None, status: int | None = None, body: str | None = None) -> str:
    """우선순위: 네트워크 예외/HTTP 403·407·429 → 차단, 5xx/점검 문구 → 점검, 로그인 관련 문구 → 자격증명, 그 외 unknown."""
    msg = f"{type(exc).__name__}: {exc}".lower() if exc is not None else ""
    b = (body or "").lower()
    if isinstance(exc, KRXCredentialsError):
        return "자격증명"
    if any(k in msg for k in _NETWORK_EXC) or status in (403, 407, 429):
        return "차단"
    if status in (500, 502, 503, 504) or any(k in b for k in _MAINT_KW) or any(k in msg for k in ("점검", "maintenance")):
        return "점검"
    if any(k in b for k in _BLOCK_KW):
        return "차단"
    if any(k in b for k in _CRED_KW) or any(k in msg for k in ("자격 증명", "credential", "세션 미인증", "로그인 실패")):
        return "자격증명"
    return "unknown"


def diagnose_krx_failure(exc: BaseException, *, http_get: Callable[..., Any] | None = None,
                         timeout: float = DEFAULT_TIMEOUT_SEC, probe: bool = True) -> dict:
    """예외(JSONDecodeError 면 `.doc` 에 응답 본문)와 로그인 페이지 GET 프로브로 진단 딕셔너리를 만든다."""
    body = getattr(exc, "doc", None) or ""
    body_source = "exception.doc" if body else None
    status: int | None = None
    probe_error = None
    if probe:
        try:
            get = http_get
            if get is None:
                import requests
                get = requests.get
            r = get(KRX_LOGIN_PAGE, headers=PROBE_UA, timeout=timeout)
            status = getattr(r, "status_code", None)
            if not body:
                body, body_source = getattr(r, "text", "") or "", "probe:login_page"
        except Exception as pe:  # noqa: BLE001
            probe_error = f"{type(pe).__name__}: {str(pe)[:200]}"
    return {"classification": classify_krx_failure(exc, status, body), "http_status": status,
            "body_head": body_head(body), "body_source": body_source,
            "exception": f"{type(exc).__name__}: {str(exc)[:200]}", "probe_error": probe_error}


def format_diagnosis(d: dict) -> str:
    return (f"[{d.get('classification', 'unknown')}] HTTP {d.get('http_status')} | {d.get('exception')} | "
            f"body({d.get('body_source')}): {d.get('body_head') or '-'}")


def purge_pykrx_modules() -> int:
    """import 실패 후 재시도를 위해 부분 로드된 pykrx 모듈을 제거한다."""
    import sys
    names = [n for n in sys.modules if n == "pykrx" or n.startswith("pykrx.")]
    for n in names:
        del sys.modules[n]
    return len(names)


# ──────────────────────────────────────────────────────────────────────────
# 캐시 직렬화 (DataFrame / Series / list / str) — 티커 문자열('005930')과 DatetimeIndex 보존
# ──────────────────────────────────────────────────────────────────────────
def _to_jsonable(obj: Any) -> dict:
    if isinstance(obj, pd.DataFrame):
        idx_kind = "datetime" if isinstance(obj.index, pd.DatetimeIndex) else "str"
        index = [i.strftime("%Y%m%d") for i in obj.index] if idx_kind == "datetime" else [str(i) for i in obj.index]
        data = obj.astype(object).where(pd.notna(obj), None).values.tolist()
        return {"kind": "df", "columns": [str(c) for c in obj.columns], "index": index,
                "index_name": obj.index.name, "index_kind": idx_kind, "data": data}
    if isinstance(obj, pd.Series):
        return {"kind": "series", "name": obj.name, "index": [str(i) for i in obj.index],
                "index_name": obj.index.name, "data": obj.astype(object).where(pd.notna(obj), None).tolist()}
    if isinstance(obj, (list, tuple, set)):
        return {"kind": "list", "data": [str(x) for x in obj]}
    if isinstance(obj, str):
        return {"kind": "str", "data": obj}
    if obj is None:
        return {"kind": "none"}
    raise TypeError(f"cannot cache type {type(obj).__name__}")


def _from_jsonable(d: dict) -> Any:
    kind = d["kind"]
    if kind == "df":
        df = pd.DataFrame(d["data"], columns=d["columns"])
        if d["index_kind"] == "datetime":
            df.index = pd.DatetimeIndex(pd.to_datetime(d["index"], format="%Y%m%d"))
        else:
            df.index = pd.Index([str(i) for i in d["index"]], dtype="object")
        df.index.name = d.get("index_name")
        return df.infer_objects()
    if kind == "series":
        s = pd.Series(d["data"], index=pd.Index([str(i) for i in d["index"]], dtype="object"), name=d.get("name"))
        s.index.name = d.get("index_name")
        return s.infer_objects()
    if kind == "list":
        return list(d["data"])
    if kind == "str":
        return d["data"]
    if kind == "none":
        return None
    raise TypeError(f"unknown cache kind {kind}")


def _default_timeout_installed() -> bool:
    import requests
    return getattr(requests.Session.request, "_relative_movers_timeout", None) is not None


def install_default_timeout(timeout_sec: float) -> None:
    """pykrx 는 requests 호출에 timeout 을 주지 않는다. 프로세스 전역으로 기본 timeout 을 주입한다(한 번만)."""
    import requests
    if _default_timeout_installed():
        return
    original = requests.Session.request

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", timeout_sec)
        return original(self, method, url, **kwargs)

    request._relative_movers_timeout = timeout_sec  # type: ignore[attr-defined]
    requests.Session.request = request  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────────
# Fetcher
# ──────────────────────────────────────────────────────────────────────────
class Fetcher:
    """pykrx 호출 래퍼. 테스트에서는 `pykrx_ns`(stock, core 네임스페이스)와 `sleep_fn`, `http_post` 를 주입한다."""

    def __init__(self, config: Mapping, *, cache_dir: str | Path | None = None,
                 pykrx_ns: Any = None, sleep_fn: Callable[[float], None] | None = None,
                 http_post: Callable[..., Any] | None = None, http_get: Callable[..., Any] | None = None,
                 env: Mapping[str, str] | None = None):
        fcfg = dict(config.get("fetch", {}) or {})
        self.sleep_sec = float(fcfg.get("sleep_sec", 1))
        self.max_retries = int(fcfg.get("max_retries", 3))
        self.import_backoff_sec = float(fcfg.get("import_retry_backoff_sec", DEFAULT_IMPORT_BACKOFF_SEC))
        self._http_get = http_get
        self.timeout_sec = max(float(fcfg.get("timeout_sec", DEFAULT_TIMEOUT_SEC)), 15.0)
        self.cache_dir = Path(cache_dir or fcfg.get("cache_dir", "data/cache"))
        self._ns = pykrx_ns
        self._sleep = sleep_fn if sleep_fn is not None else (lambda sec: time.sleep(sec))  # 호출 시점에 time.sleep 해석 (테스트 패치 가능)
        self._http_post = http_post
        self._env = env if env is not None else os.environ
        self.stats = {"network_calls": 0, "cache_hits": 0, "retries": 0}
        self.kind_debug: dict = {}   # 마지막 KIND 관리종목 응답 진단 (validate_fetch 가 기록)

    # ── pykrx 지연 import (재시도·백오프·진단) ─────────────────────────────
    def _import_pykrx(self):
        """실제 import. pykrx 는 import 시점에 KRX 로그인을 수행하며 HTML 응답이면 JSONDecodeError 를 던진다."""
        import types
        from pykrx import stock
        from pykrx.website.comm import auth
        from pykrx.website.krx.market import core
        self._check_authenticated(auth)
        return types.SimpleNamespace(stock=stock, core=core)

    @staticmethod
    def _check_authenticated(auth_module) -> None:
        """pykrx 는 로그인 실패(자격증명 오류 등)를 print 만 하고 넘어간다. 세션이 미인증이면 여기서 실패로 만든다."""
        sess = auth_module.get_auth_session()
        if sess is None or not getattr(sess, "is_authenticated", False):
            raise RuntimeError("KRX 로그인 실패: 세션 미인증 (자격 증명을 확인하세요)")

    def _diagnose(self, exc: BaseException) -> dict:
        return diagnose_krx_failure(exc, http_get=self._http_get, timeout=self.timeout_sec)

    def _pykrx(self):
        if self._ns is not None:
            return self._ns
        if not (self._env.get("KRX_ID") and self._env.get("KRX_PW")):
            raise KRXCredentialsError(
                "KRX_ID / KRX_PW 환경변수가 없습니다. pykrx>=1.2 는 KRX Data Marketplace 로그인이 필요합니다 (DESIGN.md 4절·9절).")
        install_default_timeout(self.timeout_sec)
        diag: dict = {}
        for attempt in range(1, self.max_retries + 1):
            try:
                self._ns = self._import_pykrx()      # 로그인은 stats.network_calls 에 세지 않는다 (데이터 호출 통계만)
                return self._ns
            except Exception as e:  # noqa: BLE001  (JSONDecodeError = KRX 가 HTML 응답, 네트워크 예외 등)
                self.stats["retries"] += 1
                diag = self._diagnose(e)
                log.warning("pykrx import/KRX 로그인 실패 (attempt %d/%d): %s", attempt, self.max_retries, format_diagnosis(diag))
                purge_pykrx_modules()
                if attempt < self.max_retries:
                    self._sleep(self.import_backoff_sec * attempt)
        raise KRXUnavailableError(
            f"pykrx import/KRX 로그인 {self.max_retries}회 실패 {format_diagnosis(diag)}", diagnosis=diag)

    # ── 캐시 + 재시도 ────────────────────────────────────────────────────
    def _cache_path(self, date_key: str, endpoint: str, *args: Any) -> Path:
        arg_str = "_".join(str(a) for a in args) or "none"
        arg_str = re.sub(r"[^0-9A-Za-z가-힣_\-]", "-", arg_str)
        return self.cache_dir / date_key / f"{endpoint}__{arg_str}.json"

    def _cached_call(self, date_key: str, endpoint: str, args: tuple, fn: Callable[[], Any]) -> Any:
        path = self._cache_path(date_key, endpoint, *args)
        if path.exists():
            self.stats["cache_hits"] += 1
            return _from_jsonable(json.loads(path.read_text(encoding="utf-8")))
        result = self._retry(f"{endpoint}{args}", fn)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_to_jsonable(result), ensure_ascii=False, default=str), encoding="utf-8")
        return result

    def _retry(self, label: str, fn: Callable[[], Any]) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                t0 = time.perf_counter()
                out = fn()
                self.stats["network_calls"] += 1
                log.debug("%s ok %.2fs", label, time.perf_counter() - t0)
                self._sleep(self.sleep_sec)
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                self.stats["retries"] += 1
                log.warning("%s 실패 (attempt %d/%d): %s: %s", label, attempt, self.max_retries, type(e).__name__, str(e)[:200])
                if attempt < self.max_retries:
                    self._sleep(self.sleep_sec * attempt)
        diag = self._diagnose(last)
        log.error("%s %d회 실패 — %s", label, self.max_retries, format_diagnosis(diag))
        raise KRXUnavailableError(f"{label} {self.max_retries}회 실패 {format_diagnosis(diag)}", diagnosis=diag) from last

    # ── 거래일 ───────────────────────────────────────────────────────────
    def nearest_business_day(self, date: str, prev: bool = True) -> str:
        st = self._pykrx().stock
        return self._cached_call(date, "nearest_bday", (int(prev),),
                                 lambda: st.get_nearest_business_day_in_a_week(date, prev=prev))

    @property
    def nearest(self) -> Callable[[str, bool], str]:
        """calendar.py 의 `NearestBday` 콜러블."""
        return lambda date, prev=True: self.nearest_business_day(date, prev)

    # ── 유니버스 입력 ────────────────────────────────────────────────────
    def listed(self, T: str, market: str) -> pd.DataFrame:
        """T 시점 상장 종목: index=티커, name=종목명, sect=소속부 (KRX 전종목시세, `get_market_ticker_list` 와 같은 엔드포인트)."""
        ns = self._pykrx()
        raw = self._cached_call(T, "listed", (market,), lambda: ns.core.전종목시세().fetch(T, MARKET_ID[market]))
        raw = raw.set_index("ISU_SRT_CD")
        return listed_frame(raw["ISU_ABBRV"], raw["SECT_TP_NM"])

    def etf_tickers(self, T: str) -> list[str]:
        st = self._pykrx().stock
        return self._cached_call(T, "etf_tickers", (), lambda: st.get_etf_ticker_list(T))

    def etn_tickers(self, T: str) -> list[str]:
        st = self._pykrx().stock
        return self._cached_call(T, "etn_tickers", (), lambda: st.get_etn_ticker_list(T))

    def administrative_tickers(self, T: str, market: str, listed: pd.DataFrame | None = None) -> set[str] | None:
        """관리종목 집합. 실패·미지원이면 None (universe.py 가 경고 후 미적용).

        결정(docs/administrative_issue.md, 2026-09-08 실측):
          1. KIND 관리종목 현황(POST, 시장 무관) → 파싱 성공 시 T 시점 상장 목록과 교집합
          2. KOSDAQ 은 전종목시세 소속부('관리종목') 로 판별 가능 (실측 129종목)
          3. KOSPI 는 소속부가 비어 있어 KIND 실패 시 None
        """
        if listed is None:
            listed = self.listed(T, market)
        kind = self._kind_administrative_codes(T)
        if kind is not None:
            return kind & set(listed.index)
        if market == "KOSDAQ":
            return administrative_from_sect(listed)
        log.warning("[%s] 관리종목 소스 없음 (KIND 실패, 소속부 미표기) — None 반환", market)
        return None

    def _kind_administrative_codes(self, T: str) -> set[str] | None:
        """KIND 관리종목 현황 페이지(POST)에서 종목코드를 추출. 실패·0건이면 None."""
        def fetch() -> str:
            post = self._http_post
            if post is None:
                import requests
                post = requests.post
            data = {"method": "searchAdminIssueSub", "forward": "adminissue_sub", "currentPageSize": "3000",
                    "pageIndex": "1", "orderMode": "1", "orderStat": "A", "marketType": "", "searchCorpName": "",
                    "repIsuSrtCd": ""}
            r = post(KIND_ADMIN_URL, data=data, headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout_sec)
            status = getattr(r, "status_code", 200)
            if status != 200:
                raise RuntimeError(f"KIND HTTP {status}")
            return r.text
        try:
            html = self._cached_call(T, "kind_admin", (), fetch)
        except KRXUnavailableError as e:
            log.warning("KIND 관리종목 조회 실패: %s", e)
            return None
        codes = set(KIND_CODE_RE.findall(html or ""))
        self.kind_debug = {"length": len(html or ""), "n_codes": len(codes), "head": (html or "")[:400]}
        if not codes:
            log.warning("KIND 관리종목 페이지에서 종목코드를 찾지 못함 (length=%d)", len(html or ""))
            return None
        return codes

    # ── 시세 ─────────────────────────────────────────────────────────────
    def index_ohlcv(self, fromdate: str, todate: str, market_or_code: str) -> pd.DataFrame:
        code = INDEX_CODE.get(market_or_code, market_or_code)
        st = self._pykrx().stock
        return self._cached_call(todate, "index_ohlcv", (fromdate, code), lambda: st.get_index_ohlcv(fromdate, todate, code))

    def market_cap(self, T: str, market: str) -> pd.DataFrame:
        st = self._pykrx().stock
        return self._cached_call(T, "market_cap", (market,), lambda: st.get_market_cap(T, market=market))

    def price_change(self, fromdate: str, todate: str, market: str, adjusted: bool = True) -> pd.DataFrame:
        """`get_market_price_change(from, T)`: 시가=기준가(from 직전 거래일 종가), 종가=close(T). KRX 요청 4회 발생."""
        st = self._pykrx().stock
        return self._cached_call(todate, "price_change", (fromdate, market, int(adjusted)),
                                 lambda: st.get_market_price_change(fromdate, todate, market=market, adjusted=adjusted))

    def ohlcv_adjusted_naver(self, fromdate: str, todate: str, ticker: str) -> pd.DataFrame:
        """[미사용 예외 경로] 네이버 수정주가 OHLCV. 1단계에서 price_change 가 액면분할을 반영함이 확인되어 쓰지 않는다."""
        st = self._pykrx().stock
        return self._cached_call(todate, "ohlcv_naver", (fromdate, ticker),
                                 lambda: st.get_market_ohlcv(fromdate, todate, ticker, adjusted=True))

    # ── 유틸 ─────────────────────────────────────────────────────────────
    def clear_cache(self, date_key: str | None = None) -> int:
        """캐시 삭제. date_key 가 없으면 전체. 삭제한 파일 수 반환."""
        base = self.cache_dir / date_key if date_key else self.cache_dir
        n = 0
        if base.exists():
            for f in base.rglob("*.json"):
                f.unlink()
                n += 1
        return n


def today_yyyymmdd() -> str:
    return dt.date.today().strftime("%Y%m%d")
