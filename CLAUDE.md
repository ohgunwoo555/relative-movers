# CLAUDE.md — relative-movers 작업 지침

## 0. 최우선 원칙
**이 프로젝트의 모든 정의·규칙은 `DESIGN.md`를 따른다.**
기준일(T), 기간 시작일, 수익률·초과수익률 공식, 유니버스 제외 규칙, 결과 스키마 등
핵심 정의는 DESIGN.md 2절·3절·7절이 유일한 출처다.
정의를 바꿔야 하면 **코드보다 DESIGN.md를 먼저 수정**하고, 그다음 코드·config·테스트를 맞춘다.
DESIGN.md와 코드가 충돌하면 DESIGN.md가 옳고 코드가 버그다.

## 1. 프로젝트 한 줄 요약
매일 직전 거래일(T) 기준으로 KOSPI·KOSDAQ 각각에서 시장수익률 대비 초과수익률이
가장 높은/낮은 종목을 5개 기간(1d, 1w, 1m, 6m, 1y)별로 상위 N·하위 N 산출한다.
결과 = 2시장 × 5기간 × 2방향 = 20개 랭킹.

## 2. 반드시 지킬 정의 (DESIGN.md 2절 요약, 원문이 우선)
- **T**: 실행 시점 기준 직전 거래일. 실행일이 휴장일이면 즉시 종료.
- **기간 구간 `[from, T]`**: 기준 날짜는 1d = T, 1w/1m/6m/1y = T-7일/T-1개월/T-6개월/T-1년.
  from = 기준 날짜 **이후** 가장 가까운 거래일 (1d는 from = T).
- **기준가**: from 직전 거래일의 종가(수정주가) = `get_market_price_change(from, T)`의 `시가`.
- **종목 수익률**: `close(T) / 기준가 - 1`, **수정주가 기준**.
- **시장 수익률**: KOSPI 지수 `1001`, KOSDAQ 지수 `2001`의 `idx(T) / idx(from 직전 거래일) - 1`.
- **초과수익률**: `stock_ret - market_ret` (%p). **랭킹 정렬 키.**
- **산출 개수**: 상위 N, 하위 N (`config.yaml`의 `top_n`, 기본 20).

## 3. 설정 (config.yaml)
런타임 파라미터는 `config.yaml`에서 읽는다. 코드에 하드코딩하지 않는다.
- `markets`, `periods`, `top_n`
- `exclude.*` (etn, spac, preferred, administrative, suspended) — ETF·신규상장은 항상 제외(고정)
- `filters.min_avg_trading_value` (0이면 미적용)
- `fetch.sleep_sec` / `fetch.max_retries` / `fetch.cache_dir`
- `output.dir` / `output.formats` / `output.sqlite_path`
- `notify.enabled` / `notify.channel`

## 4. 데이터 소스 규칙 (DESIGN.md 4절)
- 1순위 `pykrx`. 사용 함수: `get_market_price_change`, `get_index_ohlcv`,
  `get_market_ticker_list` / `get_etf_ticker_list`, `get_market_cap`,
  `get_nearest_business_day_in_a_week`.
- 호출 간 `sleep(1)`, 실패 시 3회 재시도, 응답은 `data/cache/`에 일자별 저장.
  pykrx 내부에는 sleep이 없으므로 래퍼(`fetch.py`)에서 넣는다.
- `get_market_price_change`의 액면분할(수정주가) 반영 여부는 1단계 검증 결과에 따른다.
  미반영이면 `get_market_ohlcv(ticker, adjusted=True)`로 해당 종목만 재계산하는 예외 경로를 둔다.
- 2순위 대체: KIS Open API.

### 4-1. 1단계 검증에서 확인된 pykrx 1.2.8 특성 (docs/stage1_validation.md 참고)
- **KRX 로그인 필수**: 환경변수 `KRX_ID`, `KRX_PW` (KRX Data Marketplace 계정). config.yaml에 넣지 않는다.
- **import 시점 로그인**: 자격증명이 있고 KRX에 닿지 못하면 `from pykrx import stock`이 예외를 던진다.
  pykrx는 함수 안에서 지연 import하고 예외를 잡아 "KRX 접근 불가"로 처리한다.
- `get_market_price_change(..., adjusted=True)`는 호출 1회당 KRX 요청 4회를 발생시킨다.
- `get_market_price_change`의 `시가` 컬럼은 시작일 **기준가**(from 직전 거래일 종가)이며 DESIGN.md 2절 정의와 동일하다.
  1단계 스크립트가 `base_price_matches_prev_close`로 실측 확인한다.
- `get_market_ohlcv(adjusted=True)`의 실제 소스는 KRX가 아니라 **네이버**(`fchart.stock.naver.com`)다.
- 액면분할 검증 기본 종목: 포스코스틸리온 058430 (KOSPI, 10:1, 신주상장 2026-04-23).
- 1단계 실행 스크립트: `python scripts/validate_stage1.py` (원격 컨테이너에서는 KRX 호스트가 차단되어 로컬에서 실행),
  또는 GitHub Actions `validate_stage1` 워크플로를 수동 실행(Secrets `KRX_ID`/`KRX_PW` 필요).
- 시크릿: GitHub Actions는 리포지토리 Secrets, launchd는 git 밖의 환경변수 파일 (DESIGN.md 9절).

## 5. 프로젝트 구조 / 모듈 책임 (DESIGN.md 5절)
```
src/calendar.py   T 및 기간 시작일 계산
src/universe.py   유니버스 + 제외 필터
src/fetch.py      pykrx 래퍼 (캐시·재시도·sleep)
src/calc.py       수익률·초과수익률
src/rank.py       상/하위 N
src/report.py     CSV/JSON/Markdown
src/notify.py     Slack 발송 (선택)
src/main.py       처리 흐름 (DESIGN.md 6절)
data/cache/, data/movers.db, outputs/YYYY-MM-DD/, tests/
```
모듈 경계를 넘는 책임을 섞지 않는다 (예: fetch.py에서 랭킹 계산 금지).

## 6. 결과 스키마 (DESIGN.md 7절)
long format. 컬럼: `base_date, market, period, direction, rank, ticker, name,
start_date, start_close, end_close, stock_ret, market_ret, excess_ret, market_cap, trading_value`.
SQLite 테이블 `movers`는 동일 스키마, PK = `(base_date, market, period, direction, rank)`.

## 7. 구현 순서 (DESIGN.md 10절)
단계를 건너뛰지 않는다. 현재 단계와 다음 단계는 아래를 갱신한다.
1. 환경·데이터 검증 (pykrx 5종 호출, 액면분할 검증, 소요시간) — 결과: `docs/stage1_validation.md`
2. calendar.py + universe.py + 테스트
3. fetch.py (캐시·재시도)
4. calc.py + rank.py — (KOSPI, 1d) 한 조합 먼저 끝까지, 이후 루프 확장
5. report.py + main.py — 20개 랭킹 통합 출력, SQLite 저장
6. notify.py + 스케줄러
7. 백필 (선택)

## 8. 개발 관례
- Python 3.10+, 의존성은 `requirements.txt`에 고정.
- 테스트는 `tests/`에 pytest. 네트워크 호출은 캐시 픽스처로 대체하고 실제 KRX 호출 테스트는 별도 마크.
- 날짜는 내부적으로 `YYYYMMDD` 문자열(pykrx 규약)로 통일하고, 출력 스키마에서는 `YYYY-MM-DD`.
- 엣지 케이스(신규상장·거래정지·상장폐지·액면분할)는 DESIGN.md 8절을 따른다.
- `data/`, `outputs/`는 git에 커밋하지 않는다 (`.gitignore`).
