#!/usr/bin/env python3
"""docs/results/<kind>_result.json → GitHub Job Summary 용 마크다운 (워크플로 Summarize 스텝에서 호출)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "results"


def calls_table(r: dict) -> None:
    print("\n| call | ok | sec | rows |\n|---|---|---|---|")
    for c in r.get("calls", []):
        print(f"| {c['call']} | {c['ok']} | {c['sec']} | {c['rows']} |")


def main(kind: str) -> int:
    path = RESULTS / f"{kind}_result.json"
    if not path.exists():
        return 1
    r = json.loads(path.read_text(encoding="utf-8"))
    if r.get("failure") or r.get("error"):
        f = r.get("failure") or {}
        print(f"### ❌ KRX 접근 실패 — 분류: **{f.get('classification', 'unknown')}**")
        print(f"- HTTP {f.get('http_status')} · {f.get('exception')}")
        print(f"- 응답 앞부분({f.get('body_source')}): `{(f.get('body_head') or '-')[:300]}`")
        if f.get("probe_error"):
            print(f"- 프로브 오류: `{f['probe_error']}`")
        print(f"- error: `{str(r.get('error'))[:500]}`")
        print()
    print(f"- T = {r.get('T')} · total {r.get('total_sec')}s" + (f" · {r['note']}" if r.get("note") else ""))
    if kind == "stage1":
        sc = r.get("split_check") or {}
        print(f"- split verdict: **{sc.get('verdict')}** ({sc.get('ticker')} {sc.get('name_resolved') or ''})")
        print(f"- 1d definition check: **{(r.get('definition_check') or {}).get('verdict')}**")
        calls_table(r)
    elif kind == "universe":
        print(f"- ETF {r.get('n_etf')} · ETN {r.get('n_etn')} · stage1 compare: `{r.get('stage1_compare')}`")
        for mkt, m in (r.get("markets") or {}).items():
            if "steps" in m:
                print(f"- **{mkt}**: " + " → ".join(f"{k}={v}" for k, v in m["steps"]))
                print(f"  - 소속부: `{m.get('sect_value_counts')}`")
                print(f"  - 우선주 규칙 충돌: `{m.get('preferred_rule_conflicts', [])[:10]}`")
                if m.get("preferred_vs_basic_info"):
                    print(f"  - 기본정보 대조: `{m['preferred_vs_basic_info']}`")
                for w in m.get("warnings", []):
                    print(f"  - ⚠️ {w}")
        bi = r.get("basic_info") or {}
        for mkt, b in bi.items():
            print(f"- 기본정보 {mkt}: 소속부 `{b.get('SECT_TP_NM')}` · 주식종류 `{b.get('KIND_STKCERT_TP_NM')}`")
        print(f"- KIND 관리종목 페이지(GET): `{r.get('kind_admin_page')}`")
    elif kind == "fetch":
        print(f"- ETF {r.get('n_etf')} · ETN {r.get('n_etn')} · windows `{r.get('windows')}`")
        for mkt, m in (r.get("markets") or {}).items():
            if "steps" in m:
                print(f"- **{mkt}**: " + " → ".join(f"{k}={v}" for k, v in m["steps"]))
                print(f"  - 관리종목: `{m.get('administrative')}`")
                print(f"  - 1w price_change rows {m.get('n_price_change_1w')} (상장폐지 -100 행 {m.get('n_delisted_rows')})")
                for w in m.get("warnings", []):
                    print(f"  - ⚠️ {w}")
        print(f"- cache check: `{r.get('cache_check')}` · stats `{r.get('fetch_stats')}`")
        print(f"- KIND debug: `{r.get('kind_debug')}`")
        print(f"- compare previous: `{r.get('compare_previous')}`")
        calls_table(r)
    elif kind == "calc":
        w = r.get("window") or {}
        c = r.get("calc") or {}
        print(f"- **{r.get('market')} {r.get('period')}** · from {w.get('from')} · 기준가일 {w.get('base_date')} · "
              f"market_ret {c.get('market_ret')}% · calc rows {c.get('n_rows')} · top_n {r.get('top_n')}")
        u = r.get("universe") or {}
        if u.get("steps"):
            print("- universe: " + " → ".join(f"{k}={v}" for k, v in u["steps"]))
        for wmsg in u.get("warnings", []):
            print(f"  - ⚠️ {wmsg}")
        print(f"- inputs: `{r.get('inputs')}` · excess_ret stats `{c.get('excess_ret_stats')}`")
        t = r.get("tables") or {}
        if t:
            print(f"\n### up {r.get('top_n')}\n{t.get('up')}\n\n### down {r.get('top_n')}\n{t.get('down')}")
        print("\n| step | sec |\n|---|---|")
        for st in r.get("steps", []):
            print(f"| {st['step']} | {st['sec']} |")
    elif kind == "main":
        print(f"- combos {sum(1 for c in r.get('combos', []) if c.get('ok'))}/{len(r.get('combos', []))} 성공 · movers rows {r.get('n_movers_rows')} · "
              f"exit {r.get('exit_code')} · fetch `{r.get('fetch_stats')}`")
        print("\n| market | period | ok | from | 기준가일 | market_ret | rows | price_change(s) | index(s) | error |\n|---|---|---|---|---|---|---|---|---|---|")
        for c in r.get("combos", []):
            w = c.get("window") or {}
            tmg = c.get("timings") or {}
            mr = c.get("market_ret")
            print(f"| {c['market']} | {c['period']} | {'✅' if c.get('ok') else '❌'} | {w.get('from', '-')} | {w.get('base_date', '-')} | "
                  f"{(f'{mr:+.2f}%' if mr is not None else '-')} | {c.get('n_calc_rows')} | {tmg.get('price_change', '-')} | "
                  f"{tmg.get('index_ohlcv', '-')} | {(c.get('error_class') or '') + ' ' + (c.get('error') or '')[:80]} |")
        for w in r.get("warnings", []):
            print(f"- ⚠️ {w}")
        for f in r.get("failures", []):
            print(f"- ❌ {f['market']} {f['period']}: [{f.get('error_class')}] {f.get('error')}")
        print(f"- 공통 타이밍: `{r.get('timings')}` · paths `{r.get('paths')}`")
        md = RESULTS / "movers.md"
        if md.exists():
            print("\n---\n")
            print(md.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "stage1"))
