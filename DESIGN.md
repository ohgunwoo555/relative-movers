# relative-movers — 시장 대비 상대 등락 종목 일별 산출기

## 1. 목적
매일 직전 거래일(T) 기준으로 KOSPI·KOSDAQ 각각에서 시장수익률 대비 가장 많이 오른/내린 종목을
5개 기간(1d, 1w, 1m, 6m, 1y)별로 산출한다. 결과 = 2시장 × 5기간 × 2방향 = 20개 랭킹.

## 2. 핵심 정의 (변경 시 반드시 이 문서부터 수정)

| 항목 | 정의 |
|---|---|
| 기준일 T | 실행 시점 기준 직전 거래일. 실행일이 휴장일이면 즉시 종료 |
| 기간 구간 | `[from, T]`. 기준 날짜: 1d = T / 1w = T-7일 / 1m = T-1개월 / 6m = T-6개월 / 1y = T-1년 |
| 구간 시작일 from | 기준 날짜 **이후** 가장 가까운 거래일 (1d는 from = T) |
| 기준가 | from **직전 거래일의 종가** (수정주가). `get_market_price_change(from, T)`의 `시가` 컬럼(BAS_PRC)과 동일 |
| 종목 수익률 | `close(T) / 기준가 - 1` (수정주가 기준). 1d는 `close(T) / close(T 직전 거래일) - 1` |
| 시장 수익률 | KOSPI 지수(1001), KOSDAQ 지수(2001): `idx(T) / idx(from 직전 거래일) - 1` — 종목과 동일 구간 |
| 초과수익률 | `stock_ret - market_ret` (%p). **랭킹 정렬 키** |
| 산출 개수 | 상위 N, 하위 N (config `top_n`, 기본 20) |

구간 방식을 `[from, T]`로 잡는 이유: pykrx `get_market_price_change`가 fromdate를 "이후 가장 가까운 거래일"로,
todate를 "이전 가장 가까운 거래일"로 보정하고 시작일 **기준가** 대비 등락률을 돌려주므로, 정의와 데이터 소스가 1:1로 대응한다.

## 3. 유니버스 및 제외 규칙
기본 유니버스: T 시점 KOSPI / KOSDAQ 상장 종목 전체.

| 제외 대상 | 기본값 | config 키 |
|---|---|---|
| ETF | 항상 제외 | (고정) |
| ETN | 제외 | `exclude.etn` |
| 스팩(SPAC) | 제외 | `exclude.spac` |
| 우선주 | 제외 | `exclude.preferred` |
| 관리종목 | 제외 | `exclude.administrative` |
| 거래정지(기간 중 거래량 0) | 제외 | `exclude.suspended` |
| 신규상장(시작일 데이터 없음) | 항상 제외 | (고정) |
| 기간 평균 거래대금 하한 | 0 (미적용) | `filters.min_avg_trading_value` |

## 4. 데이터 소스
1순위 `pykrx`:
- `get_market_price_change(fromdate, todate, market)` — 기간 내 전 종목 시가/종가/등락률
- `get_index_ohlcv(fromdate, todate, "1001"|"2001")` — 지수
- `get_market_ticker_list(date, market)`, `get_etf_ticker_list(date)` — 유니버스/ETF 제외
- `get_market_cap(date, market)` — 시총·거래대금
- `get_nearest_business_day_in_a_week(date)` — 거래일 보정

주의:
- **KRX 로그인 필수** (pykrx ≥ 1.2): KRX Data Marketplace 계정을 환경변수 `KRX_ID`, `KRX_PW`로 공급한다.
  config.yaml·코드에 자격증명을 넣지 않는다. 세션은 1시간 만료, pykrx가 자동 재로그인한다.
- **지연 import**: pykrx는 import 시점에 로그인하며 KRX에 닿지 못하면 import 자체가 예외를 던진다.
  `fetch.py`는 pykrx를 함수 안에서 import하고 예외를 잡아 "KRX 접근 불가"로 처리한다(main.py가 트레이스백으로 죽지 않게).
- **래퍼 sleep**: pykrx 내부에는 호출 간 sleep이 없다. `fetch.py`에서 호출 간 `sleep(1)`, 실패 시 3회 재시도, 응답은 `data/cache/`에 일자별 저장.
  `get_market_price_change` 1회는 KRX 요청 4회(거래일 보정 2 + 조회 2)를 발생시킨다.
- `get_market_price_change(fromdate, todate, market, adjusted=True)`: `adjusted` 기본값 True. 반환 `시가` = 시작일 기준가(2절 정의).
  액면분할 반영 여부를 1단계에서 검증. 미반영이면 `get_market_ohlcv(ticker, adjusted=True)`로 해당 종목만 재계산하는 예외 경로 추가
- **예외 경로는 네이버 API**: `get_market_ohlcv(..., adjusted=True)`의 실제 소스는 KRX가 아니라 네이버 차트 API(`fchart.stock.naver.com`)다.
  KRX 장애와 독립적이지만 별도 차단·변경 가능성이 있으므로 fetch.py에서 별개의 소스로 취급한다(`adjusted=False`만 KRX).
- 2순위 대체: KIS Open API (기간별 시세). 종목별 호출이므로 로컬 DB 축적 방식 필요

## 5. 프로젝트 구조
```
relative-movers/
├── CLAUDE.md            # "DESIGN.md의 정의를 따른다" 명시
├── DESIGN.md
├── config.yaml
├── src/
│   ├── calendar.py      # T 및 기간 시작일 계산
│   ├── universe.py      # 유니버스 + 제외 필터
│   ├── fetch.py         # pykrx 래퍼 (캐시·재시도·sleep)
│   ├── calc.py          # 수익률·초과수익률
│   ├── rank.py          # 상/하위 N
│   ├── report.py        # CSV/JSON/Markdown
│   ├── notify.py        # Slack 발송 (선택)
│   └── main.py
├── data/cache/, data/movers.db
├── outputs/YYYY-MM-DD/
└── tests/
```

## 6. 처리 흐름 (main.py)
1. T 결정. 휴장일이면 로그 남기고 종료
2. 유니버스 로드 → 제외 규칙 적용
3. for market in [KOSPI, KOSDAQ] × for period in [1d,1w,1m,6m,1y]:
   - start 계산 → 지수 수익률 → 전 종목 기간 수익률 조회
   - 유니버스와 inner join (신규상장 자동 탈락)
   - 초과수익률 계산 → 거래대금 필터 → 상위 N / 하위 N
4. 20개 랭킹을 long-format 테이블로 통합
5. `outputs/T/movers.csv`, `movers.md` 저장 + SQLite 누적 + 알림

## 7. 결과 스키마 (long format)
| 컬럼 | 설명 |
|---|---|
| base_date | T |
| market | KOSPI / KOSDAQ |
| period | 1d / 1w / 1m / 6m / 1y |
| direction | up / down |
| rank | 1~N |
| ticker, name | |
| start_date, start_close, end_close | start_date = 구간 시작 거래일 from, start_close = 기준가(from 직전 거래일 종가), end_close = close(T) |
| stock_ret, market_ret | % |
| excess_ret | %p (정렬 키) |
| market_cap, trading_value | 부가정보 |

SQLite 테이블 `movers`는 동일 스키마. PK = (base_date, market, period, direction, rank).

## 8. 엣지 케이스
- 신규상장: 제외 / 거래정지: 옵션 제외 / 상장폐지: T 목록 기준이라 자연 탈락
- 액면분할·병합: 수정주가 필요 (4절 참고)
- KRX 응답 실패: 재시도 후 실패 시 알림, 캐시로 재실행 시 중복 호출 방지

## 9. 스케줄링
- 1안 GitHub Actions cron (매일 07:00 KST). KRX 해외 IP 차단 여부를 1단계에서 확인
  (`.github/workflows/validate_stage1.yml`을 workflow_dispatch로 실행)
- 2안 launchd (Mac). 로컬 실행
- main.py는 동일, 실행 환경만 다름
- **시크릿 관리**: `KRX_ID`/`KRX_PW`(및 Slack 토큰)는
  GitHub Actions → 리포지토리 **Secrets**(`secrets.KRX_ID`, `secrets.KRX_PW`)를 `env`로 주입,
  launchd → git에 넣지 않는 **환경변수 파일**(예: `~/.config/relative-movers/env`, `.gitignore`의 `.env`)을
  plist `EnvironmentVariables` 또는 실행 래퍼 스크립트에서 `source`하여 공급. 코드·config·로그에 자격증명을 남기지 않는다

## 10. 구현 단계 (Claude Code 프롬프트 단위)
1. 환경·데이터 검증: pykrx 함수 5종 실제 호출, 액면분할 종목 검증, 소요시간 측정
2. calendar.py + universe.py + 테스트
3. fetch.py (캐시·재시도)
4. calc.py + rank.py — (KOSPI, 1d) 한 조합 먼저 끝까지, 이후 루프 확장
5. report.py + main.py — 20개 랭킹 통합 출력, SQLite 저장
6. notify.py + 스케줄러
7. 백필 (선택)
