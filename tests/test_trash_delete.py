#!/usr/bin/env python3
"""Быстрый тест _delete_trash_request: после перемещения письма в корзину
сообщение-запрос («Переместить это письмо в корзину?») с кнопками удаляется;
при ошибке удаления бот не падает."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/StudioProjects/hunttech-bot-common"))
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import bot

pass_count = 0
fail_count = 0


def check(name, cond, detail=""):
    global pass_count, fail_count
    if cond:
        pass_count += 1
        print(f"  OK  {name}")
    else:
        fail_count += 1
        print(f"  FAIL {name} {detail}")


class FakeMessage:
    def __init__(self, fail=False):
        self.fail = fail
        self.deleted = False

    async def delete(self):
        if self.fail:
            raise RuntimeError("boom")
        self.deleted = True


async def main():
    # 1. Обычный случай: запрос удалён, возвращает True
    m = FakeMessage()
    ok = await bot._delete_trash_request(m)
    check("1.1 delete вызван", m.deleted)
    check("1.2 возвращает True", ok is True)

    # 2. Ошибка удаления: не падаем, возвращаем False
    m = FakeMessage(fail=True)
    ok = await bot._delete_trash_request(m)
    check("2.1 не падает при ошибке", True)
    check("2.2 возвращает False", ok is False)

    print(f"\nИтог: {pass_count} passed, {fail_count} failed")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    asyncio.run(main())
