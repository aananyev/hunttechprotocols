#!/usr/bin/env python3
"""Быстрый тест _remove_wiki_proc_button: кнопка wiki убирается только
у того сообщения, где нажата; остальные кнопки и сообщения не трогаем."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/StudioProjects/hunttech-bot-common"))
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
    def __init__(self, reply_markup):
        self.reply_markup = reply_markup
        self.edited_kb = "NOT_CALLED"

    async def edit_reply_markup(self, reply_markup=None):
        self.edited_kb = reply_markup


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wiki_btn(txt, cb):
    return InlineKeyboardButton(text=txt, callback_data=cb)


async def main():
    # 1. Список с известным промптом: wiki + Кратко → остаётся только Кратко
    m = FakeMessage(kb([
        [wiki_btn("📝 Расшифровать и разместить в wiki #1", "wiki_proc:abc:1")],
        [wiki_btn("📌 Кратко #1", "brief:abc:1")],
    ]))
    await bot._remove_wiki_proc_button(m)
    rows = m.edited_kb.inline_keyboard if m.edited_kb != "NOT_CALLED" else None
    check("1.1 edit вызван", m.edited_kb != "NOT_CALLED")
    check("1.2 wiki-кнопки нет", rows and all(not (b.callback_data or "").startswith("wiki_proc:") for r in rows for b in r))
    check("1.3 Кратко осталась", rows and any(b.callback_data == "brief:abc:1" for r in rows for b in r))

    # 2. Клавиатура без промпта: Выбрать промпт + wiki + Кратко → wiki убрана, две остались
    m = FakeMessage(kb([
        [wiki_btn("🟡 Выбрать промпт #2", "choose_prompt:xyz:2")],
        [wiki_btn("📝 Расшифровать и разместить в wiki #2", "wiki_proc:xyz:2")],
        [wiki_btn("📌 Кратко #2", "brief:xyz:2")],
    ]))
    await bot._remove_wiki_proc_button(m)
    rows = m.edited_kb.inline_keyboard
    cbs = [b.callback_data for r in rows for b in r]
    check("2.1 wiki убрана", "wiki_proc:xyz:2" not in cbs)
    check("2.2 Выбрать промпт осталась", "choose_prompt:xyz:2" in cbs)
    check("2.3 Кратко осталась", "brief:xyz:2" in cbs)

    # 3. Только wiki-кнопка (старый формат после Саммари) → клавиатура полностью убрана
    m = FakeMessage(kb([[wiki_btn("📝 Расшифровать и разместить в wiki", "wiki_proc:3")]]))
    await bot._remove_wiki_proc_button(m)
    check("3.1 клавиатура убрана (None)", m.edited_kb is None)

    # 4. Другое сообщение без wiki-кнопки → не трогаем
    m = FakeMessage(kb([[wiki_btn("📌 Кратко #4", "brief:def:4")]]))
    await bot._remove_wiki_proc_button(m)
    check("4.1 edit не вызван", m.edited_kb == "NOT_CALLED")

    # 5. Сообщение вообще без клавиатуры → не падаем
    m = FakeMessage(None)
    await bot._remove_wiki_proc_button(m)
    check("5.1 edit не вызван", m.edited_kb == "NOT_CALLED")

    # 6. _mark_wiki_proc_busy: нажатая кнопка помечается ⏳, остальные не трогаем
    m = FakeMessage(kb([
        [wiki_btn("📝 Расшифровать и разместить в wiki #1", "wiki_proc:abc:1")],
        [wiki_btn("📌 Кратко #1", "brief:abc:1")],
    ]))
    changed = await bot._mark_wiki_proc_busy(m)
    rows = m.edited_kb.inline_keyboard
    wbtns = [b for r in rows for b in r if (b.callback_data or "").startswith(("wiki_proc:", "wiki_process:"))]
    check("6.1 mark: edit вызван", m.edited_kb != "NOT_CALLED")
    check("6.2 mark: текст с ⏳", len(wbtns) == 1 and wbtns[0].text == "⏳ Расшифровать и разместить в wiki #1",
          rows)
    check("6.3 mark: callback_data сохранён", wbtns and wbtns[0].callback_data == "wiki_proc:abc:1")
    check("6.4 mark: Кратко не тронута",
          any(b.callback_data == "brief:abc:1" and b.text == "📌 Кратко #1" for r in rows for b in r))
    check("6.5 mark: возвращён оригинал",
          changed == [("wiki_proc:abc:1", "📝 Расшифровать и разместить в wiki #1")], changed)

    # 7. _restore_wiki_proc_button: текст возвращается после ошибки
    await bot._restore_wiki_proc_button(m, changed)
    rows = m.edited_kb.inline_keyboard
    wbtns = [b for r in rows for b in r if (b.callback_data or "").startswith("wiki_proc:")]
    check("7.1 restore: текст восстановлен", wbtns and wbtns[0].text == "📝 Расшифровать и разместить в wiki #1",
          rows)

    # 8. Кнопка уведомления (wiki_process:) тоже помечается
    m = FakeMessage(kb([[wiki_btn("📝 Расшифровать и разместить в wiki", "wiki_process:key1")]]))
    changed = await bot._mark_wiki_proc_busy(m)
    check("8.1 wiki_process помечена ⏳",
          changed == [("wiki_process:key1", "📝 Расшифровать и разместить в wiki")], changed)

    # 9. Повторное нажатие (кнопка уже ⏳) — не меняем, changed пуст
    m = FakeMessage(kb([[wiki_btn("⏳ Расшифровать и разместить в wiki #1", "wiki_proc:abc:1")]]))
    changed = await bot._mark_wiki_proc_busy(m)
    check("9.1 повторное нажатие — changed пуст", changed == [], changed)
    check("9.2 повторное нажатие — edit не вызван", m.edited_kb == "NOT_CALLED")

    # 10. restore с пустым списком — не падаем, edit не вызван
    m = FakeMessage(kb([[wiki_btn("⏳ Расшифровать и разместить в wiki #1", "wiki_proc:abc:1")]]))
    await bot._restore_wiki_proc_button(m, [])
    check("10.1 restore пустой — edit не вызван", m.edited_kb == "NOT_CALLED")

    # 11. Без клавиатуры — mark/restore не падают
    m = FakeMessage(None)
    changed = await bot._mark_wiki_proc_busy(m)
    check("11.1 mark без клавиатуры — changed пуст", changed == [])
    await bot._restore_wiki_proc_button(m, [("wiki_proc:1", "old")])
    check("11.2 restore без клавиатуры — не падаем", True)

    print(f"\nИтог: {pass_count} passed, {fail_count} failed")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    asyncio.run(main())
