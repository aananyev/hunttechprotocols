"""SetupDbHandler — FSM wizard for configuring database connection.

Admin-only command: /setup db

Allows the master admin to:
1. View current DB config
2. Enter/update DATABASE-URL
3. Set pool_min, pool_max, sslmode
4. Test the connection
5. Save or discard

Regular users NEVER see this command in menu or /help.
If they type it directly, they get "access denied".
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.filters.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from hunttech_bot_common.database import DatabasePool, PoolConfig
from hunttech_bot_common.services.db_config_service import CONFIG_KEYS, DbConfigService

logger = logging.getLogger(__name__)


# ── FSM States ────────────────────────────────────────────────

class SetupDbStates(StatesGroup):
    """FSM states for /setup db wizard."""
    url = State()
    pool_min = State()
    pool_max = State()
    sslmode = State()
    confirm = State()


# ── Keyboard helpers ──────────────────────────────────────────

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="db:cancel")],
        ]
    )

def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="\u2705 \u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c", callback_data="db:save"),
                InlineKeyboardButton(text="\U0001f504 \u0422\u0435\u0441\u0442", callback_data="db:test"),
            ],
            [InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="db:cancel")],
        ]
    )

SSLMODE_BUTTONS: list[list[InlineKeyboardButton]] = [
    # disable ПЕРВЫМ: для локальной PG11 без SSL prefer/require не работают
    # (asyncpg: "rejected SSL upgrade", фолбэка нет)
    [InlineKeyboardButton(text="disable (PG11 без SSL)", callback_data="sslmode:disable")],
    [InlineKeyboardButton(text="prefer (default)", callback_data="sslmode:prefer")],
    [InlineKeyboardButton(text="require", callback_data="sslmode:require")],
    [InlineKeyboardButton(text="verify-ca", callback_data="sslmode:verify-ca")],
    [InlineKeyboardButton(text="verify-full", callback_data="sslmode:verify-full")],
    [InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="db:cancel")],
]

SSLMODE_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=SSLMODE_BUTTONS)

# Placeholder used in examples (avoid `***` pattern which triggers secret masking)
_PWD_PLACEHOLDER = "YOUR_PASSWORD"


# ── Command handler ───────────────────────────────────────────

async def cmd_setup_db(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    access_manager: Any,
    config_service: DbConfigService | None = None,
) -> None:
    """Handle /setup db, /setup db test, /setup db show (admin-only)."""
    user_id = message.from_user.id

    if not access_manager or not access_manager.is_admin(user_id):
        await message.answer("\U0001f6ab *\u041a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430 \u0442\u043e\u043b\u044c\u043a\u043e \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443.*")
        return

    args = (command.args or "").strip().lower()

    if config_service is None:
        config_service = DbConfigService()

    # /setup db show — show current config
    if args == "db show":
        await _cmd_db_show(message, config_service)
        return

    # /setup db test — test connection
    if args == "db test":
        await _cmd_db_test(message, config_service)
        return

    # /setup db — start FSM wizard
    if args == "db":
        current = config_service.load()
        display = config_service.format_config_display(current)

        await state.set_state(SetupDbStates.url)
        await state.update_data(config_service=config_service)

        url_example = "postgresql://user_name:" + _PWD_PLACEHOLDER + "@host:5432/database_name"

        await message.answer(
            f"{display}\n\n"
            "\U0001f4dd *\u0412\u0432\u0435\u0434\u0438\u0442\u0435 DATABASE-URL* \u0434\u043b\u044f \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f \u043a PostgreSQL.\n\n"
            "\u0424\u043e\u0440\u043c\u0430\u0442:\n"
            f"`{url_example}`\n\n"
            "\u0414\u043b\u044f \u0443\u0434\u0430\u043b\u0451\u043d\u043d\u043e\u0433\u043e \u0441\u0435\u0440\u0432\u0435\u0440\u0430 \u0443\u043a\u0430\u0436\u0438\u0442\u0435 \u0445\u043e\u0441\u0442 \u0438 \u043f\u043e\u0440\u0442.\n"
            "\u0415\u0441\u043b\u0438 \u043d\u0443\u0436\u043d\u043e \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0430\u0441\u0442\u044c \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u043e\u0432 \u2014 "
            "\u0432\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 URL \u0438\u043b\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 `/skip` \u0447\u0442\u043e\u0431\u044b \u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u0442\u0435\u043a\u0443\u0449\u0438\u0439.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return

    # Unknown subcommand
    await message.answer(
        "\U0001f6ab *\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u0430\u044f \u043f\u043e\u0434\u043a\u043e\u043c\u0430\u043d\u0434\u0430.*\n\n"
        "\u0414\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0435 \u043a\u043e\u043c\u0430\u043d\u0434\u044b:\n"
        "\u2022 `/setup db` \u2014 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f \u043a \u0411\u0414\n"
        "\u2022 `/setup db test` \u2014 \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435\n"
        "\u2022 `/setup db show` \u2014 \u043f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0442\u0435\u043a\u0443\u0449\u0443\u044e \u043a\u043e\u043d\u0444\u0438\u0433\u0443\u0440\u0430\u0446\u0438\u044e"
    )


# ── /setup db show ──────────────────────────────────────────


async def _cmd_db_show(message: Message, config_service: DbConfigService) -> None:
    """Show current database configuration (admin-only)."""
    config = config_service.load()
    display = config_service.format_config_display(config)

    if config:
        text = (
            f"{display}\n\n"
            "\U0001f4cb *\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435:*\n"
            "\u2022 `/setup db` \u2014 \u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438\n"
            "\u2022 `/setup db test` \u2014 \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435"
        )
    else:
        text = display + "\n\n" + "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 `/setup db` \u0447\u0442\u043e\u0431\u044b \u043d\u0430\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435."

    await message.answer(text)


# ── /setup db test ──────────────────────────────────────────


async def _cmd_db_test(message: Message, config_service: DbConfigService) -> None:
    """Test database connection (admin-only, standalone)."""
    config = config_service.load()
    if not config or not config.get("url"):
        await message.answer(
            "\u274c *\u0411\u0430\u0437\u0430 \u0434\u0430\u043d\u043d\u044b\u0445 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u0430.*\n"
            "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 `/setup db` \u0434\u043b\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438."
        )
        return

    url = config["url"]
    sslmode = config.get("sslmode", "prefer")

    host_part = "..."
    if "@" in url and "/" in url.split("@")[1]:
        host_part = url.split("@")[1].split("/")[0]

    status_msg = await message.answer(
        "\U0001f50c *\u0422\u0435\u0441\u0442\u0438\u0440\u0443\u044e \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u043a \u0431\u0430\u0437\u0435 \u0434\u0430\u043d\u043d\u044b\u0445...*\n"
        f"\u0425\u043e\u0441\u0442: {host_part}\n"
        f"SSL: {sslmode}\n\n"
        "\u23f3 \u041f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, \u043f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435..."
    )

    try:
        url_with_params = f"{url}?sslmode={sslmode}&connect_timeout=5&pool_min=1&pool_max=1"
        config_obj = PoolConfig.from_url(url_with_params)
        pool = DatabasePool(config_obj)
        await pool.connect()

        health = await pool.health_check()
        await pool.close()

        if health.get("status") == "connected":
            latency = health.get("latency_ms", "?")
            result_text = (
                "\u2705 *\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u0443\u0441\u043f\u0435\u0448\u043d\u043e!*\n\n"
                f"\u2022 \u0421\u0442\u0430\u0442\u0443\u0441: {health['status']}\n"
                f"\u2022 \u0417\u0430\u0434\u0435\u0440\u0436\u043a\u0430: {latency} \u043c\u0441"
            )
        else:
            result_text = (
                f"\u26a0\ufe0f *\u0421\u0442\u0430\u0442\u0443\u0441: {health.get('status', 'unknown')}*\n"
                f"\u041e\u0448\u0438\u0431\u043a\u0430: {health.get('error', 'unknown')}"
            )
    except Exception as e:
        logger.warning("DB connection test failed: %s", e)
        result_text = (
            "\u274c *\u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f:*\n"
            f"`{e}`\n\n"
            "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435:\n"
            "\u2022 \u0414\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u043b\u0438 \u0445\u043e\u0441\u0442\n"
            "\u2022 \u041f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u043e\u0441\u0442\u044c URL\n"
            "\u2022 \u041f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u044b SSL\n"
            "\u2022 \u041d\u0435 \u0431\u043b\u043e\u043a\u0438\u0440\u0443\u0435\u0442 \u043b\u0438 \u0444\u0430\u0439\u0440\u0432\u043e\u043b"
        )

    await status_msg.edit_text(result_text)


# ── FSM Step: URL ─────────────────────────────────────────────

async def setup_db_url(message: Message, state: FSMContext) -> None:
    """Handle URL input step."""
    text = message.text.strip()

    if text.lower() == "/skip":
        data = await state.get_data()
        config_service: DbConfigService = data.get("config_service", DbConfigService())
        current = config_service.load()
        if current and current.get("url"):
            await state.update_data(url=current["url"])
            await state.set_state(SetupDbStates.pool_min)
            await message.answer(
                "\u2705 URL \u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d \u0431\u0435\u0437 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439.\n\n"
                "\U0001f4dd *\u0412\u0432\u0435\u0434\u0438\u0442\u0435 минимальное количество соединений в пуле* "
                "(\u043f\u043e умолчанию 2, обычно 1\u20135):",
                reply_markup=_cancel_kb(),
            )
            return

    if not text.startswith("postgresql://"):
        url_example = "postgresql://user:***@host:5432/db"
        await message.answer(
            "\u274c *\u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0444\u043e\u0440\u043c\u0430\u0442.* "
            "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 DATABASE-URL:\n\n"
            "\u0424\u043e\u0440\u043c\u0430\u0442:\n"
            f"`{url_example}`\n\n"
            "\u0413\u0434\u0435:\n"
            "\u2022 `user` \u2014 \u0438\u043c\u044f \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f\n"
            "\u2022 `password` \u2014 \u043f\u0430\u0440\u043e\u043b\u044c\n"
            "\u2022 `host` \u2014 \u0430\u0434\u0440\u0435\u0441 \u0441\u0435\u0440\u0432\u0435\u0440\u0430\n"
            "\u2022 `5432` \u2014 \u043f\u043e\u0440\u0442\n"
            "\u2022 `db` \u2014 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0431\u0430\u0437\u044b \u0434\u0430\u043d\u043d\u044b\u0445\n\n"
            "\u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0451 \u0440\u0430\u0437 \u0438\u043b\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 `/skip`:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return


    await state.update_data(url=text)
    await state.set_state(SetupDbStates.pool_min)
    await message.answer(
        "\u2705 URL \u043f\u0440\u0438\u043d\u044f\u0442.\n\n"
        "\U0001f4dd *\u0412\u0432\u0435\u0434\u0438\u0442\u0435 минимальное количество соединений в пуле* "
        "(\u043f\u043e умолчанию 2, обычно 1\u20135):",
        reply_markup=_cancel_kb(),
    )


# ── FSM Step: Pool min ────────────────────────────────────────

async def setup_db_pool_min(message: Message, state: FSMContext) -> None:
    """Handle pool_min input step."""
    text = message.text.strip()

    if text.lower() == "/skip":
        await state.update_data(pool_min=2)
        await state.set_state(SetupDbStates.pool_max)
        await message.answer(
            "\u2705 Min pool оставлен по умолчанию (2).\n\n"
            "\U0001f4dd *\u0412\u0432\u0435\u0434\u0438\u0442\u0435 максимальное количество соединений в пуле* "
            "(\u043f\u043e умолчанию 10, обычно 5\u201320):",
            reply_markup=_cancel_kb(),
        )
        return

    try:
        value = int(text)
        if value < 1:
            raise ValueError
        await state.update_data(pool_min=value)
        await state.set_state(SetupDbStates.pool_max)
        await message.answer(
            f"\u2705 Min pool: `{value}`.\n\n"
            f"\U0001f4dd *\u0412\u0432\u0435\u0434\u0438\u0442\u0435 максимальное количество соединений в пуле* "
            f"(\u043f\u043e умолчанию 10, обычно 5\u201320):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
    except (ValueError, TypeError):
        await message.answer(
            "\u274c \u0412\u0432\u0435\u0434\u0438\u0442\u0435 положительное целое число (например, `2`) "
            "\u0438\u043b\u0438 `/skip` для значения по умолчанию:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )


# ── FSM Step: Pool max ────────────────────────────────────────

async def setup_db_pool_max(message: Message, state: FSMContext) -> None:
    """Handle pool_max input step."""
    text = message.text.strip()

    if text.lower() == "/skip":
        await state.update_data(pool_max=10)
        await state.set_state(SetupDbStates.sslmode)
        await message.answer(
            "\u2705 Max pool оставлен по умолчанию (10).\n\n"
            "\U0001f512 *\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 режим SSL:*\n"
            "\U0001f4a1 Для локальной PostgreSQL 11 без SSL выберите *disable* "
            "(prefer/require падают с «rejected SSL upgrade»).",
            reply_markup=SSLMODE_KEYBOARD,
        )
        return

    try:
        value = int(text)
        if value < 1:
            raise ValueError
        data = await state.get_data()
        pool_min = data.get("pool_min", 2)
        if value < pool_min:
            await message.answer(
                f"\u274c Max pool ({value}) не может быть меньше min pool ({pool_min}).\n"
                f"\u0412\u0432\u0435\u0434\u0438\u0442\u0435 значение \u2265 {pool_min} "
                f"\u0438\u043b\u0438 `/skip`:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
            return
        await state.update_data(pool_max=value)
        await state.set_state(SetupDbStates.sslmode)
        await message.answer(
            f"\u2705 Max pool: `{value}`.\n\n"
            f"\U0001f512 *\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 режим SSL:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=SSLMODE_KEYBOARD,
        )
    except (ValueError, TypeError):
        await message.answer(
            "\u274c \u0412\u0432\u0435\u0434\u0438\u0442\u0435 положительное целое число (например, `10`) "
            "\u0438\u043b\u0438 `/skip` для значения по умолчанию:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )


# ── FSM Step: SSL mode (callback) ─────────────────────────────

async def setup_db_sslmode(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle SSL mode selection callback."""
    await callback.answer()

    if not callback.data or not callback.data.startswith("sslmode:"):
        return

    sslmode = callback.data.split(":", 1)[1]
    valid_modes = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}

    if sslmode not in valid_modes:
        await callback.message.edit_text(
            "\u274c \u041d\u0435\u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u044b\u0439 режим SSL. Попробуйте ещё раз:",
            reply_markup=SSLMODE_KEYBOARD,
        )
        return

    await state.update_data(sslmode=sslmode)
    await state.set_state(SetupDbStates.confirm)

    data = await state.get_data()
    config_service: DbConfigService = data.get("config_service", DbConfigService())

    url = data.get("url", "")
    pool_min = data.get("pool_min", 2)
    pool_max = data.get("pool_max", 10)
    masked_url = config_service._mask_db_url(url)

    preview = (
        f"\U0001f50d *\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 конфигурацию:*\n\n"
        f"\u2022 `URL`: `{masked_url}`\n"
        f"\u2022 `Min pool`: `{pool_min}`\n"
        f"\u2022 `Max pool`: `{pool_max}`\n"
        f"\u2022 `SSL mode`: `{sslmode}`\n\n"
        f"\u0427\u0442\u043e делать?"
    )

    await callback.message.edit_text(preview, reply_markup=_confirm_kb())


# ── Confirmation callbacks ────────────────────────────────────

async def setup_db_test(callback: CallbackQuery, state: FSMContext) -> None:
    """Test database connection with current config."""
    await callback.answer()
    data = await state.get_data()

    url = data.get("url", "")
    sslmode = data.get("sslmode", "prefer")

    host_part = "..."
    if "@" in url and "/" in url.split("@")[1]:
        host_part = url.split("@")[1].split("/")[0]

    await callback.message.edit_text(
        "\U0001f50c *\u0422\u0435\u0441\u0442\u0438\u0440\u0443\u044e подключение к базе данных...*\n"
        f"\u0425\u043e\u0441\u0442: {host_part}\n"
        f"SSL: {sslmode}\n\n"
        "\u23f3 \u041f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, подождите..."
    )

    try:
        url_with_params = f"{url}?sslmode={sslmode}&connect_timeout=5&pool_min=1&pool_max=1"

        config_obj = PoolConfig.from_url(url_with_params)
        pool = DatabasePool(config_obj)
        await pool.connect()

        health = await pool.health_check()
        await pool.close()

        if health.get("status") == "connected":
            latency = health.get("latency_ms", "?")
            result_text = (
                "\u2705 *\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 успешно!*\n\n"
                f"\u2022 \u0421\u0442\u0430\u0442\u0443\u0441: {health['status']}\n"
                f"\u2022 \u0417\u0430\u0434\u0435\u0440\u0436\u043a\u0430: {latency} \u043c\u0441\n"
                f"\u2022 \u041f\u0443\u043b: {health.get('pool_stats', {})}\n\n"
                "\u041a\u043e\u043d\u0444\u0438\u0433\u0443\u0440\u0430\u0446\u0438\u044f корректна. Сохранить?"
            )
        else:
            result_text = (
                f"\u26a0\ufe0f *\u0421\u0442\u0430\u0442\u0443\u0441: {health.get('status', 'unknown')}*\n"
                f"\u041e\u0448\u0438\u0431\u043a\u0430: {health.get('error', 'unknown')}"
                "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 параметры подключения."
            )
    except Exception as e:
        logger.warning("DB connection test failed: %s", e)
        result_text = (
            "\u274c *\u041e\u0448\u0438\u0431\u043a\u0430 подключения:*\n"
            f"`{e}`\n\n"
            "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435:\n"
            "\u2022 \u0414\u043e\u0441\u0442\u0443\u043f\u0435\u043d ли хост\n"
            "\u2022 \u041f\u0440\u0430\u0432\u0438\u043b\u044c\u043d\u043e\u0441\u0442\u044c URL\n"
            "\u2022 \u041f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u044b SSL\n"
            "\u2022 \u041d\u0435 блокирует ли файрвол\n\n"
            "\u0418\u0441\u043f\u0440\u0430\u0432\u044c\u0442\u0435 данные или нажмите \u00ab\u041e\u0442\u043c\u0435\u043d\u0430\u00bb."
        )

    await callback.message.edit_text(
        result_text + "\n\n\u0427\u0442\u043e делать дальше?",
        reply_markup=_confirm_kb(),
    )


async def setup_db_save(callback: CallbackQuery, state: FSMContext) -> None:
    """Save database configuration."""
    await callback.answer()

    data = await state.get_data()
    config_service: DbConfigService = data.get("config_service", DbConfigService())

    config = {
        "url": data.get("url", ""),
        "pool_min": data.get("pool_min", 2),
        "pool_max": data.get("pool_max", 10),
        "sslmode": data.get("sslmode", "prefer"),
        "connect_timeout": 10,
    }

    if config_service.save(config):
        await callback.message.edit_text(
            "\u2705 *\u041a\u043e\u043d\u0444\u0438\u0433\u0443\u0440\u0430\u0446\u0438\u044f \u0411\u0414 сохранена!*\n\n"
            "\u0422\u0435\u043f\u0435\u0440\u044c все боты HuntTech будут использовать "
            "\u044d\u0442\u0438 настройки для подключения к базе данных.\n\n"
            "\u0414\u043b\u044f применения может потребоваться перезапуск бота."
        )
    else:
        await callback.message.edit_text(
            "\u274c *\u041e\u0448\u0438\u0431\u043a\u0430 сохранения конфигурации.*\n"
            "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 права на запись в директорию data/."
        )

    await state.clear()


async def setup_db_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel the setup wizard."""
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        "\u274c *\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 \u0411\u0414 отменена.*\n"
        "\u0422\u0435\u043a\u0443\u0449\u0430\u044f конфигурация не изменена."
    )


# ── Standard command definition ───────────────────────────────

def get_setup_db_command_def() -> Any:
    """Get CommandDef for /setup."""
    from hunttech_bot_common.telegram import CommandDef
    return CommandDef(
        command="setup",
        title="\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438",
        description="\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 настройками бота",
        emoji="\u2699\ufe0f",
        permissions=set(),
        admin=False,
        hidden=False,
        group="settings",
    )


__all__ = [
    "SetupDbStates",
    "cmd_setup_db",
    "setup_db_url",
    "setup_db_pool_min",
    "setup_db_pool_max",
    "setup_db_sslmode",
    "setup_db_save",
    "setup_db_test",
    "setup_db_cancel",
    "get_setup_db_command_def",
]
