"""Slack 알림 — DESIGN.md 6절 5단계 (선택).

- Slack Incoming Webhook URL 은 환경변수 `SLACK_WEBHOOK_URL` (GitHub Secrets). 없으면 **경고만 남기고 생략**.
- 메시지: 기준일, 시장별 수익률, 2시장×5기간 상위/하위 각 K종목(초과수익률 %p), 실패·경고 요약, 전체 표 링크.
- 실패(T 를 못 구함 / 조합 실패)에도 원인 분류가 담긴 알림을 보낸다. 휴장일(exit 3)은 보내지 않는다.

실행: python -m src.notify --result-json docs/results/main_result.json [--csv outputs/<T>/movers.csv]
                          [--run-url URL] [--results-url URL] [--top-k 5] [--dry-run]
종료코드: 0 발송 또는 생략 / 1 발송 실패(HTTP 오류)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

log = logging.getLogger(__name__)

ENV_WEBHOOK = "SLACK_WEBHOOK_URL"
DEFAULT_TOP_K = 5
SECTION_LIMIT = 2900          # Slack section text 한도 3000자


def _pct(x, unit="%") -> str:
    return "-" if x is None or pd.isna(x) else f"{x:+.2f}{unit}"


def _line(r) -> str:
    return f"{r['rank']}. {r['name']}({r['ticker']}) {_pct(r['excess_ret'], '%p')}"


def build_message(result: Mapping, movers: pd.DataFrame | None, *, run_url: str | None = None,
                  results_url: str | None = None, top_k: int = DEFAULT_TOP_K) -> dict:
    """Slack 페이로드 ({text, blocks}). result = main.RunResult.as_dict() (+ paths)."""
    T = result.get("T")
    combos = result.get("combos") or []
    failures = result.get("failures") or []
    warnings = result.get("warnings") or []
    failure = result.get("failure")
    exit_code = result.get("exit_code")
    blocks: list[dict] = []
    text_lines: list[str] = []

    # ── 실패 (T 조차 못 구함) ──
    if T is None:
        cls = (failure or {}).get("classification", "unknown")
        head = f":rotating_light: relative-movers 실패 — KRX 접근 불가 [{cls}]"
        body = (f"*분류*: {cls}\n*HTTP*: {(failure or {}).get('http_status')}\n"
                f"*예외*: {(failure or {}).get('exception')}\n"
                f"*응답 앞부분*: `{((failure or {}).get('body_head') or '-')[:300]}`")
        if run_url:
            body += f"\n<{run_url}|실행 로그>"
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": head[:150]}},
                  {"type": "section", "text": {"type": "mrkdwn", "text": body[:SECTION_LIMIT]}}]
        return {"text": f"{head}\n{body}", "blocks": blocks}

    ok_n = sum(1 for c in combos if c.get("ok"))
    status = ":white_check_mark:" if exit_code == 0 else ":warning:"
    head = f"{status} relative-movers {T} — {ok_n}/{len(combos)} 조합"
    blocks.append({"type": "header", "text": {"type": "plain_text", "text": head[:150]}})
    text_lines.append(head)

    # ── 시장별 수익률 ──
    mr_lines = []
    for market in dict.fromkeys(c["market"] for c in combos):
        parts = [f"{c['period']} {_pct(c.get('market_ret'))}" if c.get("ok") else f"{c['period']} ❌"
                 for c in combos if c["market"] == market]
        mr_lines.append(f"*{market}*: " + " · ".join(parts))
    links = []
    if results_url:
        links.append(f"<{results_url}|전체 표>")
    if run_url:
        links.append(f"<{run_url}|실행 로그>")
    mr_text = "\n".join(mr_lines) + ("\n" + " · ".join(links) if links else "")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": mr_text[:SECTION_LIMIT]}})
    text_lines.append(mr_text)

    # ── 조합별 상위/하위 K ──
    if movers is not None and len(movers):
        for c in combos:
            if not c.get("ok"):
                continue
            sub = movers[(movers["market"] == c["market"]) & (movers["period"] == c["period"])]
            up = sub[sub["direction"] == "up"].sort_values("rank").head(top_k)
            down = sub[sub["direction"] == "down"].sort_values("rank").head(top_k)
            body = (f"*{c['market']} {c['period']}* (시장 {_pct(c.get('market_ret'))}, {c.get('n_calc_rows')}종목)\n"
                    f"▲ " + " | ".join(_line(r) for _, r in up.iterrows()) + "\n"
                    f"▼ " + " | ".join(_line(r) for _, r in down.iterrows()))
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body[:SECTION_LIMIT]}})
            text_lines.append(body)

    # ── 실패·경고 ──
    issues = [f"❌ {f['market']} {f['period']}: [{f.get('error_class')}] {str(f.get('error'))[:200]}" for f in failures]
    issues += [f"⚠️ {w[:200]}" for w in warnings]
    if issues:
        body = "*실패 / 경고*\n" + "\n".join(issues)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body[:SECTION_LIMIT]}})
        text_lines.append(body)

    return {"text": "\n".join(text_lines)[:35000], "blocks": blocks[:50]}


def send_slack(webhook_url: str, payload: Mapping, *, http_post: Callable[..., Any] | None = None,
               timeout: float = 15) -> tuple[bool, str]:
    """Webhook POST. 반환 (성공 여부, 상태 설명)."""
    post = http_post
    if post is None:
        import requests
        post = requests.post
    try:
        r = post(webhook_url, json=dict(payload), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"
    status = getattr(r, "status_code", None)
    ok = status is not None and 200 <= status < 300
    return ok, f"HTTP {status} {getattr(r, 'text', '')[:200]}"


def notify(result: Mapping, movers: pd.DataFrame | None, config: Mapping, *, env: Mapping[str, str] | None = None,
           run_url: str | None = None, results_url: str | None = None, top_k: int = DEFAULT_TOP_K,
           http_post: Callable[..., Any] | None = None, dry_run: bool = False) -> dict:
    """알림 발송 판단 + 발송. 반환 {sent, reason, payload?}."""
    env = os.environ if env is None else env
    ncfg = dict(config.get("notify", {}) or {})
    if not ncfg.get("enabled", False):
        log.info("notify.enabled=false — 알림 생략")
        return {"sent": False, "reason": "disabled"}
    if result.get("T") is None and not result.get("failure"):
        log.info("휴장일 — 알림 생략")
        return {"sent": False, "reason": "holiday"}
    payload = build_message(result, movers, run_url=run_url, results_url=results_url, top_k=top_k)
    url = env.get(ENV_WEBHOOK)
    if not url:
        log.warning("%s 환경변수가 없습니다 — Slack 알림을 생략합니다 (메시지 %d블록)", ENV_WEBHOOK, len(payload["blocks"]))
        return {"sent": False, "reason": "no_webhook", "payload": payload}
    if dry_run:
        return {"sent": False, "reason": "dry_run", "payload": payload}
    ok, status = send_slack(url, payload, http_post=http_post)
    if ok:
        log.info("Slack 알림 발송 완료 (%s)", status)
    else:
        log.error("Slack 알림 발송 실패: %s", status)
    return {"sent": ok, "reason": status, "payload": payload}


def load_movers(result: Mapping, csv_path: str | None) -> pd.DataFrame | None:
    path = csv_path or (result.get("paths") or {}).get("daily_csv") or (result.get("paths") or {}).get("csv")
    if not path or not Path(path).exists():
        return None
    return pd.read_csv(path, dtype={"ticker": str}, encoding="utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--result-json", required=True)
    ap.add_argument("--csv", default=None, help="movers CSV (기본: result.paths.daily_csv → paths.csv)")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
    ap.add_argument("--run-url", default=None)
    ap.add_argument("--results-url", default=None)
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 페이로드를 출력")
    args = ap.parse_args(argv)

    import yaml
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    result = json.loads(Path(args.result_json).read_text(encoding="utf-8"))
    movers = load_movers(result, args.csv)
    out = notify(result, movers, config, run_url=args.run_url, results_url=args.results_url, top_k=args.top_k, dry_run=args.dry_run)
    if args.dry_run and out.get("payload"):
        print(out["payload"]["text"])
    print(f"notify: sent={out['sent']} reason={out['reason']}")
    return 0 if out["sent"] or out["reason"] in ("disabled", "holiday", "no_webhook", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
