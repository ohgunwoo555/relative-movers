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
- **`시가` 컬럼의 의미**: `get_market_price_change` 의 `시가` 는 KRX 응답의 `BAS_PRC`(시작일 **기준가** = from 직전 거래일 종가)다.
  DESIGN.md 2절은 이에 맞춰 구간 `[from, T]`, 기준가 = from 직전 거래일 종가, 수익률 = `close(T)/기준가 - 1` 로 정의했다
  (pykrx 는 fromdate 를 이후 가장 가까운 거래일, todate 를 이전 가장 가까운 거래일로 보정 → 정의와 1:1 대응).
  스크립트의 `definition_check` 가 1d 구간(from = T)에서 `시가 == close(T 직전 거래일)`, `종가 == close(T)`, `등락률 == 공식` 을 실측하고,
  1w 구간에서도 `시가 == close(from 직전 거래일)` 을 확인한다(`base_price_matches_prev_close`).

### 2-3. 수정주가 반영 여부 — 판정 방법(스크립트 구현)
구간 from = (분할상장일 − 40일) 이후 가장 가까운 거래일, to = min(분할상장일 + 30일, T) 이전 가장 가까운 거래일에 대해
1. `get_market_price_change(from, to, adjusted=True)` 의 등락률 (A)
2. `get_market_price_change(from, to, adjusted=False)` 의 등락률 (B, 미반영 기대값 ≈ -90%)
3. `get_market_ohlcv(ticker, adjusted=True)`(네이버 수정주가 = 정답)로 `close(to) / close(from 직전 거래일) − 1` (C)
4. `get_market_ohlcv(ticker, adjusted=False)`(KRX 원시가)로 동일 계산 (D)

판정: `|A − C| < 1%p` 면 **ADJUSTED**(반영), 아니면 **NOT_ADJUSTED** → DESIGN.md 4절의 예외 경로(`get_market_ohlcv(adjusted=True)` 종목별 재계산) 활성화.
보조: `|D − C| < 5%p` 면 구간에 분할이 없었다는 뜻이므로 경고(종목·날짜 오류 의심). `base_price_matches_adjusted_prev_close` 로 A 의 기준가가 수정 전일 종가와 같은지도 기록.

**종목코드 교차확인**: `get_market_ticker_name(ticker)` 가 `--split-name` 과 다르면 T 시점 해당 시장 전종목 등락률의 `종목명` 으로 코드를 찾아 대체하고(`ticker_resolved_by_name`),
찾지 못하면 `TICKER_MISMATCH` 로 검증을 중단한다.

오프라인 검증: 가짜 pykrx 로 10:1 분할 시나리오를 만들어 4가지 경우(반영 → ADJUSTED / 미반영 → NOT_ADJUSTED / 잘못된 코드 → 종목명으로 대체 / 없는 종목명 → TICKER_MISMATCH)와
1d·1w 기간 정의 실측이 기대대로 판정됨을 확인했다.

### 2-4. 검증 대상 종목 (최근 1년 내 액면분할)

| 종목 | 시장 | 분할 내용 | 매매정지 | 신주 변경상장(거래재개) | 근거 |
|---|---|---|---|---|---|
| **포스코스틸리온 (058430)** — 기본값 | KOSPI | 액면가 5,000→500원 (10:1), 600만주→6,000만주, 주가 약 45,000→4,500원 | 2026-04-08 ~ 04-22 | **2026-04-23** | 경북매일 2026-03-30 기사 |
| 신시웨이 (290560) — 대안 | KOSDAQ | 액면분할 | — | 2026-02-23 | 디지털투데이 |
| 와이씨켐 (112290) — 대안 | KOSDAQ | 액면분할 | — | 2026-04-29 | 톱스타뉴스 |

10:1 분할이라 미반영 시 등락률이 -90% 근처로 나와 판정이 명확하다. 종목코드는 기사 본문을 열지 못해(차단) 기억에 의존했으므로 실행 시 `종목명` 컬럼으로 교차확인할 것.

## 3. 실행 방법

### 3-1. GitHub Actions (권장 — 해외 IP 차단 여부도 함께 확인)
1. 리포지토리 Settings → Secrets and variables → Actions 에 `KRX_ID`, `KRX_PW` 등록
2. Actions 탭 → `validate_stage1` → **Run workflow** (입력값 기본: 포스코스틸리온 058430 / 2026-04-23 / KOSPI)
3. 실행 요약(Job Summary)에 판정·소요시간 표가 뜨고, artifact `stage1-validation-<run>` 에 `stage1_result.json`, `stage1_run.log` 가 올라간다
   - import 단계에서 실패하면 KRX 로그인 또는 해외 IP 차단(DESIGN.md 9절) 문제다. 로그의 예외 메시지로 구분한다

### 3-2. 로컬 (Mac)
```bash
pip install -r requirements.txt
export KRX_ID='<KRX Data Marketplace ID>'
export KRX_PW='<비밀번호>'
python scripts/validate_stage1.py                      # 기본: 포스코스틸리온 058430, 2026-04-23
# python scripts/validate_stage1.py --split-ticker 290560 --split-name 신시웨이 --split-date 20260223 --market KOSDAQ
```
- 출력: 함수별 소요시간·행수 표(stdout) + `docs/stage1_result.json` (gitignore 대상. 요약값은 이 문서 4절에 옮겨 적는다)
- `--skip-1y` 로 1년치 전종목 등락률 호출을 생략할 수 있다(가장 오래 걸리는 호출).
- 스크립트는 DESIGN.md 규칙(호출 간 sleep 1s, 실패 시 3회 재시도)을 그대로 적용하며, 부분 실패 시 exit 1, 자격증명 없음 시 exit 2.
- 더미 자격증명으로 실행해 실패 경로(import 시 로그인 실패 → 기록 → `docs/stage1_result.json` 저장 → exit 1)가 정상 동작함은 확인했다.

## 4. 실측 결과 (로컬 실행 후 기입)

| call | ok | sec | rows |
|---|---|---|---|
| _(미실행)_ | | | |

액면분할 판정: _(미실행)_ · 기간 정의 실측(`definition_check`): _(미실행)_ · 종목명 교차확인: _(미실행)_

## 5. DESIGN.md 반영 (완료)
1. 2절: 기간 구간 `[from, T]`, from = 기준 날짜 이후 가장 가까운 거래일(1d는 from = T), 기준가 = from 직전 거래일 종가, 지수도 동일 구간으로 통일.
2. 4절: KRX 로그인 필수(`KRX_ID`/`KRX_PW`), 지연 import, 래퍼 sleep, 예외 경로가 네이버 API 임을 명시.
3. 7절: `start_date` = from, `start_close` = 기준가로 의미 명확화.
4. 9절: 시크릿은 GitHub Actions Secrets / launchd 는 환경변수 파일. `validate_stage1` 워크플로로 해외 IP 차단 여부 확인.
