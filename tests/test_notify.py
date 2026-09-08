"""src/notify.py — 메시지 구성, 웹훅 없음 경고, 실패 알림, 휴장일 생략."""
from __future__ import annotations

import logging
import types

import pandas as pd
import pytest

from src.notify import build_message, notify, send_slack

CONFIG = {"notify": {"enabled": True, "channel": "slack", "top_k": 5}}


def movers():
    rows = []
    for market, period, mr in (("KOSPI", "1d", 4.61), ("KOSDAQ", "1w", -1.93)):
        for direction in ("up", "down"):
            for rank in range(1, 8):
                sign = 1 if direction == "up" else -1
                rows.append({"base_date": "2026-09-07", "market": market, "period": period, "direction": direction, "rank": rank,
                             "ticker": f"{rank:06d}", "name": f"{market[:2]}{direction}{rank}", "start_date": "2026-09-07",
                             "start_close": 100.0, "end_close": 110.0, "stock_ret": sign * 10.0, "market_ret": mr,
                             "excess_ret": sign * 10.0 - mr, "market_cap": 1e12, "trading_value": 1e9})
    return pd.DataFrame(rows)


def result_ok():
    return {"T": "20260907", "exit_code": 0,
            "combos": [{"market": "KOSPI", "period": "1d", "ok": True, "market_ret": 4.61, "n_calc_rows": 801},
                       {"market": "KOSDAQ", "period": "1w", "ok": True, "market_ret": -1.93, "n_calc_rows": 1603}],
            "failures": [], "warnings": ["[KOSPI] exclude.administrative=true 이지만 관리종목 목록이 제공되지 않음"]}


def fake_post_factory(status=200, text="ok", raise_exc=None):
    calls = []

    def post(url, json=None, timeout=None):
        if raise_exc:
            raise raise_exc
        calls.append({"url": url, "json": json, "timeout": timeout})
        return types.SimpleNamespace(status_code=status, text=text)
    post.calls = calls
    return post


def test_build_message_success_layout():
    p = build_message(result_ok(), movers(), run_url="https://run", results_url="https://md", top_k=5)
    text, blocks = p["text"], p["blocks"]
    assert blocks[0]["type"] == "header" and "relative-movers 20260907 — 2/2 조합" in blocks[0]["text"]["text"]
    assert "*KOSPI*: 1d +4.61%" in text and "*KOSDAQ*: 1w -1.93%" in text
    assert "<https://md|전체 표>" in text and "<https://run|실행 로그>" in text
    kospi = [b for b in blocks if b["type"] == "section" and "*KOSPI 1d*" in b["text"]["text"]][0]["text"]["text"]
    assert kospi.count("KOup") == 5 and kospi.count("KOdown") == 5         # 상위/하위 각 5
    assert "1. KOup1(000001) +5.39%p" in kospi and "1. KOdown1(000001) -14.61%p" in kospi
    assert "⚠️ [KOSPI] exclude.administrative=true" in text
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")


def test_build_message_partial_failure_marks_combo():
    r = result_ok(); r["exit_code"] = 1
    r["combos"][1] = {"market": "KOSDAQ", "period": "1w", "ok": False, "error": "price_change 3회 실패 [차단]", "error_class": "krx:차단"}
    r["failures"] = [{"market": "KOSDAQ", "period": "1w", "error_class": "krx:차단", "error": "price_change 3회 실패 [차단]"}]
    p = build_message(r, movers())
    assert ":warning:" in p["blocks"][0]["text"]["text"] and "*KOSDAQ*: 1w ❌" in p["text"]
    assert "❌ KOSDAQ 1w: [krx:차단] price_change 3회 실패" in p["text"]
    assert not any("*KOSDAQ 1w*" in b["text"]["text"] for b in p["blocks"] if b["type"] == "section")


def test_build_message_total_failure_has_classification():
    r = {"T": None, "exit_code": 1, "combos": [], "failures": [], "warnings": [],
         "failure": {"classification": "점검", "http_status": 503, "exception": "JSONDecodeError: Expecting value",
                     "body_head": "<html>서비스 점검 중입니다</html>"}}
    p = build_message(r, None, run_url="https://run")
    assert "KRX 접근 불가 [점검]" in p["blocks"][0]["text"]["text"]
    assert "*HTTP*: 503" in p["text"] and "서비스 점검 중입니다" in p["text"] and "<https://run|실행 로그>" in p["text"]


def test_notify_without_webhook_warns_and_skips(caplog):
    with caplog.at_level(logging.WARNING, logger="src.notify"):
        out = notify(result_ok(), movers(), CONFIG, env={}, http_post=fake_post_factory())
    assert out["sent"] is False and out["reason"] == "no_webhook" and "payload" in out
    assert any("SLACK_WEBHOOK_URL" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_notify_sends_when_webhook_present():
    post = fake_post_factory()
    out = notify(result_ok(), movers(), CONFIG, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"}, http_post=post)
    assert out["sent"] is True and post.calls[0]["url"] == "https://hooks.slack.com/x"
    assert post.calls[0]["json"]["blocks"][0]["type"] == "header" and post.calls[0]["timeout"] == 15


def test_notify_failure_result_still_sent():
    r = {"T": None, "failure": {"classification": "차단"}, "combos": [], "failures": [], "warnings": []}
    post = fake_post_factory()
    out = notify(r, None, CONFIG, env={"SLACK_WEBHOOK_URL": "u"}, http_post=post)
    assert out["sent"] and "[차단]" in post.calls[0]["json"]["text"]


def test_notify_skips_holiday_and_disabled():
    post = fake_post_factory()
    assert notify({"T": None, "note": "휴장일"}, None, CONFIG, env={"SLACK_WEBHOOK_URL": "u"}, http_post=post)["reason"] == "holiday"
    assert notify(result_ok(), movers(), {"notify": {"enabled": False}}, env={"SLACK_WEBHOOK_URL": "u"}, http_post=post)["reason"] == "disabled"
    assert post.calls == []


def test_send_slack_http_error_and_exception():
    ok, status = send_slack("u", {"text": "x"}, http_post=fake_post_factory(status=400, text="invalid_payload"))
    assert not ok and "HTTP 400" in status
    ok, status = send_slack("u", {"text": "x"}, http_post=fake_post_factory(raise_exc=ConnectionError("down")))
    assert not ok and status.startswith("ConnectionError")
    out = notify(result_ok(), movers(), CONFIG, env={"SLACK_WEBHOOK_URL": "u"}, http_post=fake_post_factory(status=500))
    assert out["sent"] is False and "HTTP 500" in out["reason"]
