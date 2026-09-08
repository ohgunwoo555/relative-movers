# 1단계 검증 보고 — 환경·데이터 검증 (DESIGN.md 10절 1단계)

작성일: 2026-09-08 · 실행 환경: Claude Code 원격 컨테이너(해외 IP, 조직 egress 정책 적용)

## 결론 요약 (2026-09-08 GitHub Actions `validate_stage1` 실측 반영)

| 항목 | 결과 |
|---|---|
| pykrx 설치 | **성공** — `pykrx==1.2.8` (Python 3.11) |
| pykrx 함수 5종 실제 호출 | **전부 성공** (exit 0). GitHub Actions `ubuntu-latest` 러너(해외 IP)에서 KRX 로그인·조회 정상 → **해외 IP 차단 없음** |
| 소요시간 | 검증 스크립트 전체 **62.7초**(1년치 전종목 등락률 생략). 대부분의 호출 0.2~1.7초, 첫 `get_index_ohlcv` 8.0초·첫 `get_market_ohlcv` 5.9초는 워밍업성 지연 |
| 액면분할 수정주가 검증 | **ADJUSTED** — 포스코스틸리온 058430(10:1, 2026-04-23) 구간 `[2026-03-16, 2026-05-22]`에서 `get_market_price_change(adjusted=True)` 등락률이 네이버 수정주가 수익률과 1%p 이내 일치. **예외 경로 불필요** |
| 기간 정의 실측 | **OK** — 1d 구간(from = T)에서 `시가 == close(T 직전 거래일)`, `종가 == close(T)`, `등락률 == 공식`. 1w 구간 기준가도 from 직전 거래일 종가와 일치 |
| 종목명 교차확인 | `get_market_ticker_name(058430) == 포스코스틸리온` 일치 |
| 실행 환경 | **GitHub Actions cron 채택** (DESIGN.md 9절 확정). launchd 는 백업 |

→ **1단계 완료.** 2단계(calendar.py + universe.py)로 진행.

## 1. 네트워크 차단 상세

| 호스트 | 용도 | 결과 |
|---|---|---|
| `data.krx.co.kr:443` | pykrx 의 모든 KRX 함수 (로그인·시세·지수·유니버스·시총) | 프록시 CONNECT 403 — `recentRelayFailures: connect_rejected (policy denial)` |
| `fchart.stock.naver.com` (http/https) | `get_market_ohlcv(adjusted=True)` 의 실제 데이터 소스 | 403 `Host not in allowlist` |
| `kind.krx.co.kr`, 뉴스 사이트 | 액면분할 공시 확인 | EGRESS_BLOCKED |

- 프록시 README 지침상 403/407 정책 거부는 재시도·우회 대상이 아니다. 위 차단은 Claude Code 원격 컨테이너에 한정된 것이며,
  GitHub Actions 러너(해외 IP)에서는 KRX 로그인·조회가 정상 동작함을 4절 실측으로 확인했다.

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
- 출력: 함수별 소요시간·행수 표(stdout) + `docs/results/stage1_result.json` (gitignore 대상. 요약값은 이 문서 4절에 옮겨 적는다)
- `--skip-1y` 로 1년치 전종목 등락률 호출을 생략할 수 있다(가장 오래 걸리는 호출).
- 스크립트는 DESIGN.md 규칙(호출 간 sleep 1s, 실패 시 3회 재시도)을 그대로 적용하며, 부분 실패 시 exit 1, 자격증명 없음 시 exit 2.
- 더미 자격증명으로 실행해 실패 경로(import 시 로그인 실패 → 기록 → `docs/results/stage1_result.json` 저장 → exit 1)가 정상 동작함은 확인했다.

## 4. 실측 결과 — GitHub Actions `validate_stage1` (2026-09-08, `skip_1y=true`)

- 실행 환경: `ubuntu-latest`, Python 3.11, pykrx 1.2.8, `secrets.KRX_ID`/`KRX_PW` 주입 · exit 0 · 총 **62.7초**
- T = **2026-09-07** (실행일 09-08 화 → 직전 거래일 월)
- 유니버스: KOSPI **943** · KOSDAQ **1,822** · ETF **1,167** 종목
- 기간 정의 실측(`definition_check`, 삼성전자 005930): **OK**
- 액면분할 판정(`split_check`, 포스코스틸리온 058430, 구간 2026-03-16 ~ 05-22, 기준가일 03-13): **ADJUSTED**
- 종목명 교차확인: 일치 (`name_mismatch = false`)
- 상세 수치(A/B/C/D 등락률, 기준가)는 artifact `stage1-validation-<run>` 의 `stage1_result.json` 참고

| call | ok | sec | rows |
|---|---|---|---|
| import pykrx (KRX login at import) | True | 2.94 | — |
| nearest_bday(20260908) | True | 0.30 | — |
| nearest_bday(20260907) | True | 0.26 | — |
| nearest_bday(20260831, prev=False) [1w from] | True | 0.25 | — |
| nearest_bday(20260807, prev=False) [1m from] | True | 0.33 | — |
| nearest_bday(20260307, prev=False) [6m from] | True | 0.36 | — |
| nearest_bday(20250907, prev=False) [1y from] | True | 0.43 | — |
| get_market_ticker_list(T, KOSPI) | True | 0.54 | 943 |
| get_market_ticker_list(T, KOSDAQ) | True | 0.70 | 1822 |
| get_etf_ticker_list(T) | True | 2.84 | 1167 |
| nearest_bday(20260830) | True | 0.26 | — |
| get_index_ohlcv(20260828, 20260907, 1001) | True | **8.03** | 7 |
| get_index_ohlcv(20260828, 20260907, 2001) | True | 0.29 | 7 |
| get_market_cap(T, KOSPI) | True | 0.53 | 943 |
| get_market_price_change(20260907, 20260907, KOSPI) [1d] | True | 1.23 | 943 |
| get_market_price_change(20260831, 20260907, KOSPI) [1w] | True | 1.48 | 943 |
| get_market_price_change(20260831, 20260907, KOSDAQ) [1w] | True | 1.63 | 1823 |
| nearest_bday(20260906) | True | 0.28 | — |
| get_market_ohlcv(20260904, 20260907, 005930, adjusted=False) | True | **5.88** | 2 |
| nearest_bday(20260830) | True | 0.24 | — |
| get_market_ohlcv(20260828, 20260831, 005930, adjusted=False) | True | 0.24 | 2 |
| get_market_ticker_name(058430) | True | 0.00 | — |
| nearest_bday(20260314, prev=False) | True | 0.33 | — |
| nearest_bday(20260523) | True | 0.28 | — |
| nearest_bday(20260315) | True | 0.43 | — |
| price_change(20260316, 20260522, KOSPI, adjusted=True) | True | 1.73 | 951 |
| price_change(20260316, 20260522, KOSPI, adjusted=False) | True | 1.62 | 951 |
| get_market_ohlcv(20260302, 20260522, 058430, adjusted=True) [naver] | True | 1.01 | 57 |
| get_market_ohlcv(20260302, 20260522, 058430, adjusted=False) [krx] | True | 0.23 | 57 |

### 4-1. 실측에서 얻은 설계 시사점
- **전종목 등락률 행수 > 티커 목록**: 1w KOSDAQ 1,823 vs 1,822, 분할 구간 KOSPI 951 vs 943. pykrx 가 구간 중 상장폐지된 종목을
  `종가 0, 등락률 -100` 행으로 덧붙이기 때문. DESIGN.md 6절의 "T 시점 유니버스와 inner join" 으로 자연 제거된다(calc 전에 join 필수).
- **본 실행 예상 비용**: 호출당 ≈1~2초 + sleep 1초. 2시장 × 5기간 전종목 등락률(10회, 각 KRX 요청 4회) + 지수 10회 + 유니버스·시총 ≈ 30회 → 약 1~2분.
  1년치 전종목 등락률은 이번에 생략했으므로 첫 본 실행에서 시간을 기록할 것(730일 미만이라 분할 조회 없음).
- **첫 호출 지연**: 엔드포인트별 첫 호출이 6~8초 걸린다. 재시도 타임아웃은 15초 이상으로 잡는다.
- **네이버 수정주가 경로도 GitHub 러너에서 정상**(1.01초). 예외 경로는 지금 불필요하지만 fetch.py 에 함수만 남겨 둔다.
- 워크플로 경고: `actions/*@v4/v5` 가 Node 20 기반이라 deprecation 경고. 동작에는 영향 없음. 6단계에서 최신 메이저로 올린다.

## 5. DESIGN.md 반영 (완료)
1. 2절: 기간 구간 `[from, T]`, from = 기준 날짜 이후 가장 가까운 거래일(1d는 from = T), 기준가 = from 직전 거래일 종가, 지수도 동일 구간으로 통일.
2. 4절: KRX 로그인 필수(`KRX_ID`/`KRX_PW`), 지연 import, 래퍼 sleep, 예외 경로가 네이버 API 임을 명시.
3. 7절: `start_date` = from, `start_close` = 기준가로 의미 명확화.
4. 9절: 시크릿은 GitHub Actions Secrets / launchd 는 환경변수 파일. `validate_stage1` 워크플로로 해외 IP 차단 여부 확인.
