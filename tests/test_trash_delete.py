#!/usr/bin/env python3
"""Быстрый тест _delete_trash_request: после перемещения письма в корзину
сообщение-запрос («Переместить это письмо в корзину?») с кнопками удаляется;
при ошибке удаления бот не падает. Плюс _fetch_email_brief_from_server:
краткое описание письма («тема» от отправителя) для сообщений бота."""
import asyncio
import base64
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


class FakeServer:
    """Имитирует imaplib: uid-команды возвращают (typ, [(метаданные, заголовки)]).."""

    def __init__(self, hdr=b"", fail=False):
        self.hdr = hdr
        self.fail = fail

    def uid(self, cmd, *args):
        if self.fail:
            raise RuntimeError("boom")
        return "OK", [(b"1 (UID 42 BODY[HEADER.FIELDS (SUBJECT FROM)] {100}", self.hdr)]

    def fetch(self, msg_id, what):
        if self.fail:
            raise RuntimeError("boom")
        return "OK", [(b"1 (BODY[HEADER.FIELDS (SUBJECT FROM)] {100}", self.hdr)]


class FakeFilterServer:
    """Имитирует IMAP для _filter_and_extract: search не нужен (список приходит
    снаружи), fetch отдаёт заголовки/полное письмо, store запоминает вызовы."""

    def __init__(self, email_bytes, uid):
        self.email_bytes = email_bytes
        self.uid = uid
        self.stored = []

    def fetch(self, msg_id, what):
        if "HEADER.FIELDS" in what:
            hdr = self.email_bytes.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
            return "OK", [(b"1 (BODY[HEADER.FIELDS (SUBJECT)] {100}", hdr)]
        return "OK", [(b"5 (UID %s BODY[] {200}" % self.uid, self.email_bytes)]

    def store(self, msg_id, *args):
        self.stored.append((msg_id, args))
        return "OK", None


class TrashServer:
    """Имитирует IMAP-сервер для _move_email_to_trash / _set_email_read:
    uid_exists=False — письма нет в INBOX (уже в корзине);
    telemost=False — письмо в INBOX, но не протокол Телемоста;
    fail=True — IMAP-ошибка; move_ok=False — UID MOVE падает."""

    def __init__(self, uid_exists=True, telemost=True, move_ok=True, fail=False):
        self.uid_exists = uid_exists
        self.telemost = telemost
        self.move_ok = move_ok
        self.fail = fail
        self.calls = []

    def select(self, folder):
        return "OK", []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Trash) "/" "Trash"']

    def uid(self, cmd, *args):
        self.calls.append((cmd, args))
        if self.fail:
            raise RuntimeError("boom")
        if cmd == "FETCH":
            if not self.uid_exists:
                return "OK", []
            if self.telemost:
                hdr = (
                    f"Subject: {mime_word('Конспект встречи: Планёрка')}\r\n"
                    "From: Хранитель встреч Телемоста <keeper@telemost.yandex.ru>\r\n\r\n"
                ).encode()
            else:
                hdr = (
                    "Subject: Пакет документов СВЯ-0427778 на оплату\r\n"
                    "From: noreply-oplata@cdek.ru\r\n\r\n"
                ).encode()
            return "OK", [(b"1 (UID 42 BODY[HEADER.FIELDS (SUBJECT FROM)] {100}", hdr)]
        if cmd == "MOVE":
            return ("OK", None) if self.move_ok else ("NO", [b"some error"])
        return "OK", None

    def expunge(self):
        return "OK", None

    def close(self):
        pass

    def logout(self):
        pass


def mime_word(text):
    return f"=?UTF-8?B?{base64.b64encode(text.encode()).decode()}?="


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

    # 3. Описание письма из заголовков (тема + отправитель)
    hdr = (
        f"Subject: {mime_word('Конспект встречи: Планёрка')}\r\n"
        f"From: {mime_word('Александр')} <a@hunttech.ru>\r\n\r\n"
    ).encode()
    brief = bot._fetch_email_brief_from_server(FakeServer(hdr), "42")
    check("3.1 тема письма в описании", "«Конспект встречи: Планёрка»" in brief, brief)
    check("3.2 отправитель в описании", "от Александр <a@hunttech.ru>" in brief, brief)

    # 4. Пустые заголовки — фолбэк на imap_msg_id
    brief = bot._fetch_email_brief_from_server(FakeServer(b""), "42")
    check("4.1 пустые заголовки → imap_msg_id", brief == "42", brief)

    # 5. Ошибка IMAP — фолбэк на imap_msg_id, не падаем
    brief = bot._fetch_email_brief_from_server(FakeServer(fail=True), "42")
    check("5.1 ошибка IMAP → imap_msg_id", brief == "42", brief)

    # 6. _filter_and_extract: imap_id — это UID (стабильный), а не seq-номер
    subj_enc = mime_word("Конспект встречи: Планёрка").encode()
    email_bytes = (
        b"From: test@hunttech.ru\r\n"
        b"Subject: " + subj_enc + b"\r\n"
        b"Message-ID: <abc@hunttech.ru>\r\n"
        b"Date: Mon, 13 Jul 2026 10:00:00 +0400\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"hello\r\n"
    )
    fs = FakeFilterServer(email_bytes, b"4567")
    matched = bot._filter_and_extract(fs, [b"5"])
    check("6.1 письмо распознано", len(matched) == 1, matched)
    check("6.2 imap_id = UID 4567 (не seq 5)", bool(matched) and matched[0][5] == "4567", matched)
    check("6.3 display из темы", bool(matched) and "Планёрка" in matched[0][1], matched)
    check("6.4 -FLAGS \\Seen снят (store вызван)", len(fs.stored) == 1, fs.stored)

    # 7. _filter_and_extract: нет UID в ответе — фолбэк на seq (не падаем)
    fs = FakeFilterServer(email_bytes, b"")
    matched = bot._filter_and_extract(fs, [b"5"])
    check("7.1 фолбэк imap_id = seq", bool(matched) and matched[0][5] == "5", matched)

    # 8. СТРАЖ: _verify_telemost_email пропускает только протоколы Телемоста
    tele_hdr = (
        f"Subject: {mime_word('Конспект встречи: Планёрка')}\r\n"
        "From: Хранитель встреч Телемоста <keeper@telemost.yandex.ru>\r\n\r\n"
    ).encode()
    ok, brief = bot._verify_telemost_email(FakeServer(tele_hdr), "42")
    check("8.1 телемост: тема+отправитель → True", ok is True, brief)
    check("8.2 телемост: brief с темой", "«Конспект встречи: Планёрка»" in brief, brief)

    # 8.3 чужой отправитель (CDEK) при теме конспекта → False (fail-closed)
    cdek_hdr = (
        f"Subject: {mime_word('Конспект встречи: Планёрка')}\r\n"
        "From: noreply-oplata@cdek.ru\r\n\r\n"
    ).encode()
    ok, _ = bot._verify_telemost_email(FakeServer(cdek_hdr), "42")
    check("8.3 чужой отправитель → False", ok is False)

    # 8.4 чужая тема при отправителе телемоста → False
    bad_subj_hdr = (
        f"Subject: {mime_word('Пакет документов СВЯ-0427778 на оплату')}\r\n"
        "From: Хранитель встреч Телемоста <keeper@telemost.yandex.ru>\r\n\r\n"
    ).encode()
    ok, _ = bot._verify_telemost_email(FakeServer(bad_subj_hdr), "42")
    check("8.4 чужая тема → False", ok is False)

    # 8.5 полное чужое письмо (тема+отправитель не телемост) → False
    full_foreign = (
        "Subject: Пакет документов СВЯ-0427778 от 26.07.2026 на оплату\r\n"
        "From: noreply-oplata@cdek.ru\r\n\r\n"
    ).encode()
    ok, _ = bot._verify_telemost_email(FakeServer(full_foreign), "42")
    check("8.5 чужое письмо целиком → False", ok is False)

    # 8.6 ошибка IMAP → False (fail-closed), brief = imap_id
    ok, brief = bot._verify_telemost_email(FakeServer(fail=True), "42")
    check("8.6 ошибка IMAP → (False, '42')", ok is False and brief == "42", brief)

    # 8.7 пустые заголовки → False (fail-closed)
    ok, brief = bot._verify_telemost_email(FakeServer(b""), "42")
    check("8.7 пустые заголовки → (False, '42')", ok is False and brief == "42", brief)

    # ── 9. _move_email_to_trash: (ok, reason, brief) ──────────────────────
    orig_connect = bot._connect_imap
    orig_guc = bot.get_user_config
    bot.get_user_config = lambda uid: {"email": "a@hunttech.ru", "server": "imap",
                                       "login": "a@hunttech.ru", "password": "x"}

    # 9.1 Письма нет в INBOX (уже в корзине) — это НЕ ошибка: reason=already_gone
    bot._connect_imap = lambda config: TrashServer(uid_exists=False)
    ok, reason, brief = bot._move_email_to_trash(1, "42")
    check("9.1 нет в INBOX → ok=False", ok is False)
    check("9.2 reason=already_gone", reason == "already_gone", reason)

    # 9.3 Письмо в INBOX и это Телемост → перемещено
    bot._connect_imap = lambda config: TrashServer(uid_exists=True, telemost=True)
    ok, reason, brief = bot._move_email_to_trash(1, "42")
    check("9.3 телемост → ok=True", ok is True)
    check("9.4 reason=moved", reason == "moved", reason)
    check("9.5 brief с темой письма", "«Конспект встречи: Планёрка»" in brief, brief)

    # 9.6 Чужое письмо в INBOX — страж: reason=not_telemost, ящик не тронут
    srv = TrashServer(uid_exists=True, telemost=False)
    bot._connect_imap = lambda config: srv
    ok, reason, brief = bot._move_email_to_trash(1, "42")
    check("9.6 не телемост → ok=False", ok is False)
    check("9.7 reason=not_telemost", reason == "not_telemost", reason)
    check("9.8 не телемост: MOVE не вызывался",
          not any(cmd == "MOVE" for cmd, _ in srv.calls))

    # 9.9 IMAP-ошибка → reason=error
    bot._connect_imap = lambda config: TrashServer(fail=True)
    ok, reason, _ = bot._move_email_to_trash(1, "42")
    check("9.9 ошибка IMAP → ok=False, reason=error", ok is False and reason == "error", reason)

    # ── 10. _set_email_read: статус-строки ────────────────────────────────
    bot._connect_imap = lambda config: TrashServer(uid_exists=True, telemost=True)
    st = bot._set_email_read(1, "42")
    check("10.1 телемост → 'ok'", st == "ok", st)

    bot._connect_imap = lambda config: TrashServer(uid_exists=False)
    st = bot._set_email_read(1, "42")
    check("10.2 нет в INBOX → 'not_available'", st == "not_available", st)

    # Ошибка чтения заголовков внутри проверки тоже fail-closed → not_available
    bot._connect_imap = lambda config: TrashServer(fail=True)
    st = bot._set_email_read(1, "42")
    check("10.3 сбой FETCH → 'not_available' (fail-closed)", st == "not_available", st)

    # Реальный сбой подключения → 'error'
    def boom_connect(config):
        raise RuntimeError("connect boom")
    bot._connect_imap = boom_connect
    st = bot._set_email_read(1, "42")
    check("10.4 ошибка подключения → 'error'", st == "error", st)

    # восстановить моки, чтобы не влиять на другие тесты
    bot._connect_imap = orig_connect
    bot.get_user_config = orig_guc

    print(f"\nИтог: {pass_count} passed, {fail_count} failed")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    asyncio.run(main())
