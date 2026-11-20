"""Сводка изменений бота при перезапуске (стандарт HuntTech).

Git-подход (эталон — @hunttech_open_close_vacancy_bot, одобрен владельцем):
маркер ``startup_state.json`` хранит SHA прошлого запуска; при старте бота
после приветствия администратору отправляется краткая сводка:

* маркера нет (первый запуск) — «📦 Последние изменения бота»
  (последние N коммитов);
* SHA изменился — «📦 Изменения с прошлого запуска» (коммиты от прошлого
  SHA до HEAD, до max_items);
* SHA тот же — молча (однократность).

Логика полностью отделена от Telegram-фреймворка: ``build_startup_changelog``
возвращает готовый текст, а ``send_startup_changelog`` умеет доставить его
и aiogram-ботом, и PTB-ботом (``bot.send_message(chat_id=..., text=...,
parse_mode=None)`` — общий API).

Usage::

    from hunttech_bot_common.services.startup import send_startup_changelog

    await send_startup_changelog(
        bot, app.master_admin_id,
        repo_dir=REPO_DIR, state_path=DATA_DIR / "startup_state.json",
    )

Требование: бот должен быть git-репозиторием (репо = источник истины).
Если git недоступен или репо отсутствует — тихий пропуск (None).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 10
"""Максимум пунктов в сводке изменений (при превышении — «… и ещё несколько»)."""

DEFAULT_FIRST_RUN_ITEMS = 8
"""Сколько последних коммитов показывать при первом запуске (маркера нет)."""

HEADER_FIRST_RUN = "📦 Последние изменения бота:"
HEADER_CHANGED = "📦 Изменения с прошлого запуска:"
HEADER_UNCOMMITTED = "📦 Незакоммиченные изменения:"


def bot_version(repo_dir: str | Path) -> str:
    """Версия бота для приветствий и сводок (стандарт HuntTech).

    Приоритет:
    1. ``version = "X.Y.Z"`` из pyproject.toml корня репозитория;
    2. короткий SHA последнего коммита (``git rev-parse --short HEAD``);
    3. "unknown" — если ничего не доступно.
    """
    try:
        pyproject = Path(repo_dir) / "pyproject.toml"
        if pyproject.exists():
            import re as _re

            m = _re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject.read_text(encoding="utf-8"), _re.M)
            if m:
                return m.group(1)
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: pyproject version не прочитан: %s", e)
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
        )
        sha = (out.stdout or "").strip()
        if sha:
            return sha
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: git short sha failed: %s", e)
    return "unknown"

# ── Git-хелперы ────────────────────────────────────────────────────


def git_sha(repo_dir: str | Path) -> str | None:
    """Текущий HEAD репозитория (или None, если git недоступен)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
        )
        sha = (out.stdout or "").strip()
        return sha or None
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: git rev-parse failed: %s", e)
        return None


def git_subjects_since(prev_sha: str, repo_dir: str | Path, max_items: int = DEFAULT_MAX_ITEMS) -> list[str]:
    """Темы коммитов от prev_sha (не включая его) до HEAD, до max_items."""
    try:
        out = subprocess.run(
            ["git", "log", "--format=%s", f"{prev_sha}..HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip()][:max_items]
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: git log failed: %s", e)
        return []


def git_recent_subjects(repo_dir: str | Path, max_items: int = DEFAULT_FIRST_RUN_ITEMS) -> list[str]:
    """Темы последних max_items коммитов HEAD (для первого запуска)."""
    try:
        out = subprocess.run(
            ["git", "log", f"-{max_items}", "--format=%s"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: git log failed: %s", e)
        return []


def git_uncommitted_changes(repo_dir: str | Path, max_items: int = DEFAULT_MAX_ITEMS) -> list[str]:
    """Незакоммиченные изменения (git status --short), до max_items строк.

    Показывает правки, которые ещё не в коммитах (modified/untracked) —
    чтобы сводка при старте не молчала, пока код меняли без коммита
    (реальная проблема: @hunttech_short_vacancy_bot, 2026-08-07).
    """
    try:
        out = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_dir),
        )
        return [l.strip() for l in out.stdout.splitlines() if l.strip()][:max_items]
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: git status failed: %s", e)
        return []


# ── Маркер прошлого запуска ────────────────────────────────────────


def load_startup_marker(state_path: str | Path) -> str | None:
    """SHA прошлого запуска из маркера (или None)."""
    try:
        st = json.loads(Path(state_path).read_text(encoding="utf-8"))
        return st.get("sha") or None
    except Exception:
        return None


def save_startup_marker(state_path: str | Path, sha: str) -> None:
    """Сохранить маркер текущего запуска (атомарно: tmp + replace)."""
    try:
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"sha": sha, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("changelog: маркер не сохранён: %s", e)


# ── Сборка сводки ──────────────────────────────────────────────────


def build_startup_changelog(
    repo_dir: str | Path,
    state_path: str | Path,
    max_items: int = DEFAULT_MAX_ITEMS,
    first_run_items: int = DEFAULT_FIRST_RUN_ITEMS,
) -> dict[str, Any] | None:
    """Собрать сводку изменений с прошлого запуска.

    Returns:
        {"header": str, "items": [str, ...]} — сводку для отправки;
        None — изменений нет (тот же SHA) или git недоступен.
        Маркер НЕ обновляется — это делает send_startup_changelog.
    """
    cur = git_sha(repo_dir)
    if not cur:
        return None
    prev = load_startup_marker(state_path)
    if prev is None:
        items = git_recent_subjects(repo_dir, max_items=first_run_items)
        return {"header": HEADER_FIRST_RUN, "items": items} if items else None
    if prev != cur:
        items = git_subjects_since(prev, repo_dir, max_items=max_items + 1)
        if items:
            return {"header": HEADER_CHANGED, "items": items}
        logger.info("changelog: prev SHA не предок HEAD (rebase/force-push?) — без сводки")
        return None
    # Тот же SHA, но есть незакоммиченные правки — показываем их
    # (иначе сводка молчит, пока код меняют без коммита).
    items = git_uncommitted_changes(repo_dir, max_items=max_items)
    if items:
        return {"header": HEADER_UNCOMMITTED, "items": items}
    return None


def format_startup_changelog(header: str, items: list[str], max_items: int = DEFAULT_MAX_ITEMS) -> str:
    """Текст сводки: шапка + пункты «• …», при превышении — «… и ещё несколько»."""
    lines = [header]
    lines += ["• " + i for i in items]
    if len(items) > max_items:
        lines = lines[:max_items + 1]
        lines.append("… и ещё несколько коммитов")
    return "\n".join(lines)


# ── Доставка (aiogram/PTB-совместимо) ─────────────────────────────


async def send_startup_changelog(
    bot: Any,
    chat_id: int,
    repo_dir: str | Path,
    state_path: str | Path,
    max_items: int = DEFAULT_MAX_ITEMS,
    first_run_items: int = DEFAULT_FIRST_RUN_ITEMS,
) -> bool:
    """Собрать и отправить сводку изменений админу (plain text, parse_mode=None).

    Вызывать ПОСЛЕ приветствия (логотип → приветствие → сводка). Версия
    бота — только в приветствии (не дублируется в сводке; требование
    владельца 2026-08: «2 раза версия — оставь первый вывод»).
    Сохраняет маркер текущего SHA. Возвращает True, если сводка отправлена;
    False — изменений нет / git недоступен / ошибка отправки.
    """
    changelog = build_startup_changelog(repo_dir, state_path, max_items, first_run_items)
    cur = git_sha(repo_dir)
    if changelog:
        text = format_startup_changelog(changelog["header"], changelog["items"], max_items)
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
            logger.info("changelog: отправлено %d пунктов админу %s (версия %s)",
                        len(changelog["items"]), chat_id, bot_version(repo_dir))
        except Exception as e:  # noqa: BLE001
            logger.warning("changelog: отправка не удалась: %s", e)
            return False
    if cur:
        save_startup_marker(state_path, cur)
    return bool(changelog)
