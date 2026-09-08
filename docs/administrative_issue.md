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

## 결정 (검증 후 기입)
- 채택 소스: _(미정)_
- fetch.py 함수: `get_administrative_tickers(T) -> set[str] | None` (None = 조회 실패/미지원 → universe.py 가 경고)
