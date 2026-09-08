# docs/results — 실행·검증 결과 (워크플로가 자동 커밋)

| 경로 | 생성 | 용도 |
|---|---|---|
| `daily/<T>.csv` | `daily` / `validate_main` | **일별 결과 원본** (DESIGN.md 7절 long format). `scripts/rebuild_db.py` 가 이 파일들로 `data/movers.db` 를 재구성 |
| `daily/<T>.md` | 〃 | 시장·기간별 상위/하위 표. Slack 알림의 "전체 표" 링크 대상 |
| `main_result.json` | 〃 | 최신 실행 요약 (조합별 상태·타이밍·실패 분류). Job Summary 와 Slack 알림 입력 |
| `stage1_result.json`, `universe_result.json`, `fetch_result.json`, `calc_result.json` | 각 `validate_*` | 최신 검증 결과. 다음 검증이 개수를 자동 대조 |

- DB(`*.db`)와 로그(`*.log`)는 커밋하지 않는다 (`.gitignore`). DB 는 `python scripts/rebuild_db.py` 로 언제든 재구성한다.
- 날짜별 JSON 사본은 두지 않는다. 이력은 `daily/` 가 담당한다.
- 수동으로 편집하지 않는다. 해석은 `docs/stage1_validation.md`, `docs/universe_validation.md`, `docs/administrative_issue.md` 에 적는다.
