# 1단계 검증 보고 — 환경·데이터 검증 (DESIGN.md 10절 1단계)

작성일: 2026-09-08 · 실행 환경: Claude Code 원격 컨테이너(해외 IP, 조직 egress 정책 적용)

## 결론 요약

| 항목 | 결과 |
|---|---|
| pykrx 설치 | **성공** — `pykrx==1.2.8` (pandas 2.3.3, numpy 2.4.6, Python 3.11) |
| pykrx 함수 5종 실제 호출 | **미완료(차단)** — `data.krx.co.kr` 가 이 세션의 네트워크 egress 정책에서 403(policy denial)으로 차단됨. 우회하지 않음 |
| 소요시간 측정 | **미완료(차단)** — 위와 동일. 측정 스크립트는 작성 완료(`scripts/validate_stage1.py`) |
| 액면분할 수정주가 검증 | **미완료(차단)** — 검증 대상 종목·기간 확정, 판정 로직 스크립트에 구현 완료 |
| 신규 발견(중요) | **pykrx ≥ 1.2 는 KRX Data Marketplace 로그인 필수** (`KRX_ID`/`KRX_PW` 환경변수). 2024-12-27 KRX 정보데이터시스템 개편에 따른 변경. DESIGN.md·config 에 반영 필요 |

→ **1단계는 이 환경에서 완료할 수 없다.** 사용자 로컬(Mac, 국내 IP) 또는 KRX 호스트가 허용된 환경에서
`scripts/validate_stage1.py` 를 한 번 실행하면 나머지 항목이 자동으로 채워진다(아래 "실행 방법").

## 1. 네트워크 차단 상세

| 호스트 | 용도 | 결과 |
|---|---|---|
| `data.krx.co.kr:443` | pykrx 의 모든 KRX 함수 (로그인·시세·지수·유니버스·시총) | 프록시 CONNECT 403 — `recentRelayFailures: connect_rejected (policy denial)` |
| `fchart.stock.naver.com` (http/https) | `get_market_ohlcv(adjusted=True)` 의 실제 데이터 소스 | 403 `Host not in allowlist` |
| `kind.krx.co.kr`, 뉴스 사이트 | 액면분할 공시 확인 | EGRESS_BLOCKED |

- 프록시 README 지침상 403/407 정책 거부는 재시도·우회 대상이 아니다. 따라서 "KRX 가 해외 IP 를 차단하는지"(DESIGN.md 9절)는
  이 세션에서 **분리해서 판정할 수 없다** — KRX 응답이 아니라 우리 쪽 egress 정책이 먼저 막았기 때문이다.
  GitHub Actions(해외 러너)에서의 접근 가능 여부는 별도 워크플로로 1회 확인이 필요하다.

## 2. pykrx 1.2.8 소스 분석으로 확인한 사실 (네트워크 불필요)

### 2-1. KRX 로그인 필수
- `pykrx/website/comm/webio.py` 는 import 시점에 `build_krx_session()` 을 호출해 `KRX_ID`/`KRX_PW` 로 로그인한다.
  환경변수가 없으면 `KRX 로그인 실패: KRX_ID 또는 KRX_PW 환경 변수가 설정되지 않았습니다.` 를 출력하고 비인증 세션으로 진행한다
  (KRX 개편 이후 비인증 조회는 실패하거나 빈 응답을 돌려줄 가능성이 높음).
- 세션은 1시간 만료, 만료 5분 전부터 자동 재로그인 (`auth.py: KRXSession.is_valid / refresh`).
- **import 시점 크래시**: `KRX_ID`/`KRX_PW` 가 설정된 상태에서 KRX 에 연결할 수 없으면 `from pykrx import stock` 자체가
  `requests.ProxyError`/`ConnectionError` 를 던진다(`auth.warmup_krx_session` 에 예외 처리 없음). 더미 자격증명으로 실제 재현함.
  → `fetch.py` 는 pykrx 를 **함수 내부에서 지연 import** 하고 예외를 잡아 "KRX 접근 불가"로 알림·종료해야 한다(main.py 가 트레이스백으로 죽지 않도록).
- 영향: GitHub Actions 는 Secrets 로, launchd 는 plist `EnvironmentVariables` 또는 `.env` 로 자격증명을 넣어야 한다.
  `fetch.py` 는 로그인 실패를 명시적으로 감지해 즉시 실패시켜야 한다(빈 DataFrame 을 "정상"으로 오인 방지).

### 2-2. 함수별 실제 데이터 소스와 내부 호출 수

| DESIGN.md 4절 함수 | 실제 소스 | 내부 HTTP 호출 수 | 비고 |
|---|---|---|---|
| `get_market_price_change(from, to, market, adjusted=True)` | KRX `전종목등락률` (MDCSTAT01602, `adjStkPrc=2`) | **4회** (`nearest_bday`×2 + `fetch`×2) | `adjusted` 파라미터 존재. 두 번째 fetch 는 상장폐지 종목 검출용(from~from) |
| `get_index_ohlcv(from, to, "1001"/"2001")` | KRX | 1회 (730일 초과 시 분할) | |
| `get_market_ticker_list(date, market)` / `get_etf_ticker_list(date)` | KRX | 각 1회 | |
| `get_market_cap(date, market)` | KRX | 1회 | 컬럼: 종가·시가총액·거래량·거래대금·상장주식수 |
| `get_nearest_business_day_in_a_week(date)` | KRX | 1회 | `prev=False` 옵션으로 다음 거래일도 가능 |
| (예외 경로) `get_market_ohlcv(from, to, ticker, adjusted=True)` | **Naver** `fchart.stock.naver.com` | 1회 | KRX 가 아니라 네이버 차트 API. `adjusted=False` 만 KRX |

- pykrx 내부에는 호출 간 sleep 이 없다(730일 분할 구간 제외). DESIGN.md 의 `sleep(1)` 은 래퍼(`fetch.py`)에서 넣어야 한다.
- **주의 — `시가` 컬럼의 의미**: `get_market_price_change` 의 `시가` 는 KRX 응답의 `BAS_PRC`(시작일 **기준가**)이며, 통상 "시작일의 전일 종가(권리 반영)" 다.
  DESIGN.md 2절 정의 `close(T)/close(start) - 1` 과 정확히 일치시키려면
  (a) `fromdate = start 의 다음 거래일` 로 호출하거나 (b) 등락률 대신 별도 종가로 재계산해야 한다.
  검증 스크립트가 `base_price_equals_first_close` 항목으로 이를 판정하도록 해두었다. **2단계(calendar.py) 전에 실행 결과로 확정할 것.**

### 2-3. 수정주가 반영 여부 — 판정 방법(스크립트 구현)
분할 전후를 포함하는 구간 `[분할상장일-40일, 분할상장일+30일]` 에 대해
1. `get_market_price_change(..., adjusted=True)` 의 등락률 (A)
2. `get_market_price_change(..., adjusted=False)` 의 등락률 (B, 미반영 기대값 ≈ -90%)
3. `get_market_ohlcv(ticker, adjusted=True)` 첫·끝 종가로 계산한 수익률 (C, 네이버 수정주가 = 정답)
4. `get_market_ohlcv(ticker, adjusted=False)` 로 계산한 수익률 (D)

판정: `|A − C| < 1%p` 면 **ADJUSTED**(반영), 아니면 **NOT_ADJUSTED** → DESIGN.md 4절의 예외 경로(`get_market_ohlcv(adjusted=True)` 종목별 재계산) 활성화.

### 2-4. 검증 대상 종목 (최근 1년 내 액면분할)

| 종목 | 시장 | 분할 내용 | 매매정지 | 신주 변경상장(거래재개) | 근거 |
|---|---|---|---|---|---|
| **포스코스틸리온 (058430)** — 기본값 | KOSPI | 액면가 5,000→500원 (10:1), 600만주→6,000만주, 주가 약 45,000→4,500원 | 2026-04-08 ~ 04-22 | **2026-04-23** | 경북매일 2026-03-30 기사 |
| 신시웨이 (290560) — 대안 | KOSDAQ | 액면분할 | — | 2026-02-23 | 디지털투데이 |
| 와이씨켐 (112290) — 대안 | KOSDAQ | 액면분할 | — | 2026-04-29 | 톱스타뉴스 |

10:1 분할이라 미반영 시 등락률이 -90% 근처로 나와 판정이 명확하다. 종목코드는 기사 본문을 열지 못해(차단) 기억에 의존했으므로 실행 시 `종목명` 컬럼으로 교차확인할 것.

## 3. 실행 방법 (로컬)

```bash
pip install -r requirements.txt
export KRX_ID='<KRX Data Marketplace ID>'
export KRX_PW='<비밀번호>'
python scripts/validate_stage1.py                      # 기본: 포스코스틸리온 058430, 2026-04-23
# python scripts/validate_stage1.py --split-ticker 290560 --split-date 20260223 --market KOSDAQ
```
- 출력: 함수별 소요시간·행수 표(stdout) + `docs/stage1_result.json` (gitignore 대상. 요약값은 이 문서 4절에 옮겨 적는다)
- `--skip-1y` 로 1년치 전종목 등락률 호출을 생략할 수 있다(가장 오래 걸리는 호출).
- 스크립트는 DESIGN.md 규칙(호출 간 sleep 1s, 실패 시 3회 재시도)을 그대로 적용하며, 부분 실패 시 exit 1, 자격증명 없음 시 exit 2.
- 더미 자격증명으로 실행해 실패 경로(import 시 로그인 실패 → 기록 → `docs/stage1_result.json` 저장 → exit 1)가 정상 동작함은 확인했다.

## 4. 실측 결과 (로컬 실행 후 기입)

| call | ok | sec | rows |
|---|---|---|---|
| _(미실행)_ | | | |

액면분할 판정: _(미실행)_ · `시가`=close(start) 여부: _(미실행)_

## 5. DESIGN.md / config 반영 제안 (사용자 확인 후 적용)
1. 4절 데이터 소스에 "pykrx ≥1.2: `KRX_ID`/`KRX_PW` 필수, 1시간 세션" 추가. config 에 자격증명은 넣지 않고 환경변수만 사용.
2. 4절 `get_market_price_change` 항목에 `adjusted=True` 명시, `시가`=기준가(전일 종가) 주의 추가.
3. 4절 예외 경로 `get_market_ohlcv(adjusted=True)` 의 소스가 **네이버**임을 명시 (KRX 장애와 독립적이라는 장점, 별도 차단 가능성이라는 단점).
4. 9절 GitHub Actions 안: KRX 로그인 + 해외 IP 두 가지를 모두 워크플로 1회 실행으로 확인.
