#!/usr/bin/env python3
"""Тесты учёта AI-вызовов (общий реестр расходов) в HuntTech Protocols Bot.

Проверяет _track_usage (запись UsageRecord в реестр) и команду /usage:
- запись ok с токенами/стоимостью
- запись error
- /usage доступен только администратору
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "hunttech-bot-common"))

import bot
from hunttech_bot_common.ai import UsageTracker

pass_count = 0
fail_count = 0


def check(name: str, cond: bool) -> None:
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print(f"PASS - {name}")
    else:
        fail_count += 1
        print(f"FAIL - {name}")


def test_track_usage_ok() -> None:
    with tempfile.TemporaryDirectory() as d:
        tracker = UsageTracker(path=Path(d) / "ai_usage.json")
        bot._usage_tracker = tracker
        bot._track_usage(272980897, "api.deepseek.com", "deepseek-chat",
                         "summarize_protocol", "ok",
                         {"prompt_tokens": 100, "completion_tokens": 50,
                          "total_tokens": 150}, 1234.0)
        rows = tracker.records("all")
        check("ok: запись создана", len(rows) == 1)
        check("ok: bot_name", rows[0]["bot_name"] == "protocols")
        check("ok: task", rows[0]["task"] == "summarize_protocol")
        check("ok: tokens", rows[0]["total_tokens"] == 150)
        check("ok: user_id", rows[0]["user_id"] == 272980897)
        check("ok: status", rows[0]["status"] == "ok")
        check("ok: cost>0", rows[0]["cost_usd"] > 0)


def test_track_usage_error() -> None:
    with tempfile.TemporaryDirectory() as d:
        tracker = UsageTracker(path=Path(d) / "ai_usage.json")
        bot._usage_tracker = tracker
        bot._track_usage(272980897, "api.deepseek.com", "deepseek-chat",
                         "brief", "error", None, 0.0)
        rows = tracker.records("all")
        check("error: запись создана", len(rows) == 1)
        check("error: status", rows[0]["status"] == "error")
        check("error: tokens=0", rows[0]["total_tokens"] == 0)


def test_usage_command_admin_gate() -> None:
    from aiogram.filters import CommandObject

    class FakeUser:
        def __init__(self, uid):
            self.id = uid

    class FakeMessage:
        def __init__(self, uid):
            self.from_user = FakeUser(uid)
            self.answers = []

        async def answer(self, text, *a, **kw):
            self.answers.append(text)

    class FakeAccess:
        def is_admin(self, uid):
            return uid == 272980897

    old = bot.access_manager
    bot.access_manager = FakeAccess()
    try:
        msg = FakeMessage(12345)
        asyncio.run(bot.cmd_usage(msg, CommandObject(command="usage")))
        check("usage: не-админ отклонён", "Только администратор" in msg.answers[0])

        with tempfile.TemporaryDirectory() as d:
            bot._usage_tracker = UsageTracker(path=Path(d) / "ai_usage.json")
            bot._track_usage(272980897, "api.deepseek.com", "deepseek-chat",
                             "summarize_protocol", "ok",
                             {"prompt_tokens": 100, "completion_tokens": 50,
                              "total_tokens": 150}, 100.0)
            msg = FakeMessage(272980897)
            asyncio.run(bot.cmd_usage(msg, CommandObject(command="usage")))
            check("usage: админ получил отчёт", len(msg.answers) > 0)
            check("usage: отчёт содержит ИТОГО", "ИТОГО" in msg.answers[0])
            check("usage: отчёт содержит задачу", "summarize_protocol" in msg.answers[0])
    finally:
        bot.access_manager = old


if __name__ == "__main__":
    test_track_usage_ok()
    test_track_usage_error()
    test_usage_command_admin_gate()
    print(f"\n📊 Итог: {pass_count} passed, {fail_count} failed")
    sys.exit(1 if fail_count else 0)
