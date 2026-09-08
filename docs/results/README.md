# docs/results — 검증 실행 결과 (워크플로가 자동 커밋)

| 파일 | 생성 워크플로 | 다음 검증에서의 용도 |
|---|---|---|
| `stage1_result.json` (+ `stage1_result_<T>.json`) | `validate_stage1` | `validate_universe`/`validate_fetch` 가 KOSPI·KOSDAQ·ETF 개수를 대조 |
| `universe_result.json` (+ `universe_result_<T>.json`) | `validate_universe` | `validate_fetch` 가 시장별 listed·ETF·ETN 개수를 대조 |
| `fetch_result.json` (+ `fetch_result_<T>.json`) | `validate_fetch` | 4단계 이후 캐시·관리종목 소스 확정 근거 |

- `<kind>_result.json` 은 항상 최신 실행, `<kind>_result_<T>.json` 은 기준일별 이력이다.
- 실행 로그(`*.log`)는 artifact 에만 올라가고 git 에는 넣지 않는다.
- 수동으로 편집하지 않는다. 해석은 `docs/stage1_validation.md`, `docs/universe_validation.md`, `docs/administrative_issue.md` 에 적는다.
