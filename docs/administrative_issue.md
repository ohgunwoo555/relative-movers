# 관리종목 판별 방법 조사 (2단계 universe.py)

작성일: 2026-09-08 · 조사 환경: Claude Code 원격 컨테이너 (KRX·KIND 호스트 차단 — 코드 조사만 가능, 실호출은 `validate_universe` 워크플로로 확인)

## 결론
1. **pykrx 1.2.8 에는 관리종목 조회 함수가 없다.** `stock_api.py` 공개 함수 전체와 소스에서 "관리/administrative" 검색 결과 0건.
2. 후보 소스는 아래 3가지. 어느 것도 이 환경에서 실호출로 검증하지 못했으므로 `scripts/validate_universe.py` 가 (a)(b)(c)를 모두 조사해 `docs/universe_result.json` 에 남긴다.
3. 검증 전까지 `src/universe.py` 는 관리종목 집합을 **호출자 인자**로 받고, `exclude.administrative: true` 인데 집합이 None 이면
   **WARNING 로그 + 결과 `warnings` 에 기록 + 단계명 `-administrative(미적용)`** 으로 미적용을 드러낸다. 조용히 넘어가지 않는다.

## 후보 소스

| | 소스 | 접근 방법 | 장점 | 불확실한 점 |
|---|---|---|---|---|
| (a) | KRX 전종목시세 `[12001] MDCSTAT01501` 의 `SECT_TP_NM`(소속부) | pykrx 내부 `전종목시세().fetch(T, "STK"/"KSQ")` — `get_market_ticker_list` 가 이미 호출하는 엔드포인트라 **추가 요청 0회**, 날짜 지정 가능 | 로그인 세션 재사용, T 시점 스냅샷 | 소속부는 KOSDAQ 체계(우량기업부/벤처기업부/…/관리종목). **KOSPI 관리종목이 여기 표기되는지 미확인** |
| (b) | KRX 전종목기본정보 `[12005] MDCSTAT01901` 의 `SECT_TP_NM`, `KIND_STKCERT_TP_NM`(주식종류) | pykrx 내부 `전종목기본정보().fetch("ALL")` 1회 | 주식종류(보통주/우선주)가 있어 **우선주 판별의 정답 데이터**로도 쓸 수 있음 | 날짜 인자 없음(현재 스냅샷만) → 백필 불가. 관리종목 표기 여부는 (a)와 같은 불확실성 |
| (c) | KIND 관리종목 현황 `kind.krx.co.kr/investwarn/adminissue.do` | HTML/엑셀 스크래핑 | KOSPI·KOSDAQ 모두 공식 목록, 지정일·사유 포함 | 로그인 세션과 별개, HTML 구조 변경 위험, GitHub 러너에서 접근 가능 여부 미확인 |

## 검증 절차 (`validate_universe` 워크플로 실행 후 이 문서 갱신)
- (a) `markets.KOSPI.sect_value_counts` / `markets.KOSDAQ.sect_value_counts` 에 `관리종목` 값이 있는가. KOSPI 에도 있으면 (a) 채택.
- (b) `basic_info.*.SECT_TP_NM` 에 `관리종목` 이 있는가, `KIND_STKCERT_TP_NM` 의 `우선주` 집합이 우리 규칙(`-preferred`)과 일치하는가 (`preferred_vs_basic_info`).
- (c) `kind_admin_page.status == 200` 이고 `contains_keyword` 가 true 이면 스크래핑 후보로 유지.
- KOSPI 관리종목이 (a)(b) 어디에도 없으면: (c) 스크래퍼를 3단계 fetch.py 에 구현하거나, 그때까지 경고 유지.

## 실측 (2026-09-08 `validate_universe`, T = 2026-09-07)

| 소스 | KOSPI | KOSDAQ |
|---|---|---|
| (a) 전종목시세 소속부 | **전부 빈 값** (`{'': 943}`) → 판별 불가 | `관리종목(소속부없음)` **129** 종목 (그 외 중견 511 / 우량 466 / 벤처 340 / 기술성장 255 / SPAC 65 / 투자주의환기 41 / 외국기업 15) |
| (b) 전종목기본정보 소속부 | 요약에 미출력 — `docs/results/universe_result.json` 의 `basic_info` 참고. KOSPI 는 (a)와 같은 컬럼 체계라 빈 값일 가능성이 높음 | (a)와 동일 체계 |
| (c) KIND 관리종목 페이지 (GET) | HTTP 200, 1,472 바이트, 키워드 없음 → **GET 은 껍데기 페이지**. 데이터는 POST(`method=searchAdminIssueSub`, `forward=adminissue_sub`)로 받아야 함 | 동일 |

## 결정
- **KOSDAQ: (a) 전종목시세 소속부** 채택. 티커 목록과 같은 요청이라 추가 호출 0회, T 시점 스냅샷.
- **KOSPI: KIND POST 스크래핑을 1순위로 시도**하고, 실패하면 **None → universe.py 가 경고 후 미적용**.
  KIND 가 동작하면 KOSDAQ 도 KIND 를 우선 사용하고 소속부는 폴백으로 둔다(공식 목록이 우선).
- 구현: `src/fetch.py` `Fetcher.administrative_tickers(T, market, listed) -> set[str] | None`
  1. `_kind_administrative_codes(T)`: `POST kind.krx.co.kr/investwarn/adminissue.do` → `companysummary_open('XXXXXX')` 패턴으로 종목코드 추출 → T 상장 목록과 교집합. 응답 캐시.
  2. 실패·0건이면 KOSDAQ 은 `administrative_from_sect(listed)`, KOSPI 는 None (WARNING).
- 검증: `validate_fetch` 워크플로가 KIND 파싱 건수, 시장별 교집합, KOSDAQ 소속부 129와의 겹침을 `docs/results/fetch_result.json` 에 남긴다.
  KIND 가 동작하지 않으면 다음 후보는 KRX 정보데이터시스템의 관리종목 화면(bld 미확인)이며, 그때까지 KOSPI 관리종목 필터는 미적용 상태가 경고로 드러난다.

## 3단계 실측 (2026-09-08 `validate_fetch`, T = 2026-09-07) — KOSPI 항목 확정
- KIND POST(`method=searchAdminIssueSub`, `forward=adminissue_sub`): 응답은 왔지만 `companysummary_open('XXXXXX')` 패턴 **0건**
  (`kind_codes_total: 0`, 4.1초). 파라미터 또는 HTML 구조가 가정과 다르다. 다음 실행부터 `kind_debug`(응답 길이·앞 400자)가 기록된다.
- KOSDAQ 소속부: **129종목**, `관리종목(소속부없음)` 값만 집계됨(투자주의환기 41·SPAC 65·외국기업 15는 미포함). 매칭을 '관리종목'으로 시작하는 값으로 한정했다.

### 확정
| 시장 | 상태 | 동작 |
|---|---|---|
| KOSDAQ | **적용** | 전종목시세 소속부 `관리종목*` (추가 호출 0회) |
| KOSPI | **미적용 (경고)** | 소속부 빈 값 + KIND 파싱 0건 → `administrative_tickers` 가 None → `-administrative(미적용)` 단계와 WARNING 으로 드러남 |

KOSPI 후속 후보 (별도 작업, 현재 파이프라인은 경고 상태로 진행):
1. `kind_debug` 로 KIND 응답 확인 후 파라미터/파서 수정 (예: `marketType=stockMkt`, 페이지 크기, 응답이 iframe/JS 렌더링인지)
2. KRX 정보데이터시스템 관리종목 화면(bld 미확인) — 로그인 세션으로 `bld` 탐색 필요
3. KOSPI 관리종목은 통상 10~20종목 수준이라 랭킹 영향이 제한적이지만, 하위 N 에 섞일 가능성이 있어 결과 리포트에 경고를 함께 출력한다

## KOSPI 후속 후보 조사: KRX 정보데이터시스템 "관리종목 현황" 화면 (2026-09-08, 조사만 — 구현은 별도)

pykrx 의 모든 KRX 조회는 같은 방식이다: `POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd` 에
`bld=dbms/MDC/STAT/standard/MDCSTATxxxxx` + 화면별 파라미터(`mktId`, `trdDd`, `strtDd/endDd` …)를 보내고 `OutBlock_1`/`output` JSON 을 받는다
(`pykrx/website/krx/krxio.py::KrxWebIo`, 로그인 세션 쿠키는 `webio.Post.read` 가 자동으로 붙인다).
따라서 관리종목 화면도 **bld 코드와 파라미터만 알면 pykrx 세션을 재사용해 한 클래스로 붙일 수 있다.**

| 항목 | 내용 |
|---|---|
| 화면 위치 | KRX 정보데이터시스템(data.krx.co.kr) → 통계 → 기본통계 → 주식 → 종목정보 → **관리종목** (`투자주의환기종목`, `거래정지종목` 화면도 같은 메뉴) |
| bld | **미확인**. pykrx 1.2.8 에 포함된 bld 는 `MDCSTAT01501`(전종목시세)·`MDCSTAT01602`(전종목등락률)·`MDCSTAT01901`(전종목기본정보) 등이며 관리종목용 코드는 없다. 이 환경은 KRX 가 차단되어 직접 확인 불가 |
| 확인 방법 | 브라우저에서 해당 화면을 열고 개발자도구 Network 탭에서 `getJsonData.cmd` 요청의 Form Data 를 본다. `bld` 값과 함께 넘어가는 파라미터(예: `mktId=STK/KSQ/ALL`, `trdDd=YYYYMMDD`, `share`, `money`, `csvxls_isNo`)를 그대로 옮기면 된다 |
| 구현 스케치 | ```python\nfrom pykrx.website.krx.krxio import KrxWebIo\nclass 관리종목현황(KrxWebIo):\n    @property\n    def bld(self): return \"dbms/MDC/STAT/standard/MDCSTATxxxxx\"  # 확인 후 기입\n    def fetch(self, trdDd, mktId=\"ALL\"):\n        return DataFrame(self.read(mktId=mktId, trdDd=trdDd)[\"OutBlock_1\"])\n``` → `Fetcher._krx_administrative_codes(T)` 로 감싸 `ISU_SRT_CD` 집합 반환, KIND 보다 앞 순위로 시도 |
| 장점 | 로그인 세션·캐시·재시도·타임아웃을 그대로 재사용, T 시점(`trdDd`) 지정 가능(백필 가능), KOSPI·KOSDAQ 동시 |
| 리스크 | bld/파라미터가 화면 개편으로 바뀔 수 있음. 응답 컬럼명(`ISU_SRT_CD` 등)도 확인 필요 |
| 검증 계획 | bld 확인 후 `validate_fetch` 에 프로브 추가: 응답 행수, KOSDAQ 소속부 129 종목과의 겹침(=정확도 근거), KOSPI 건수 |

우선순위(구현 시): ① KRX 관리종목 현황(bld 확인되면) → ② KIND POST(파라미터·파서 수정) → ③ KOSDAQ 소속부(현행). 그때까지 KOSPI 는 경고 상태.
