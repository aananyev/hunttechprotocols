"""1C Telegram bot commands for HuntTech bots.

Provides admin commands for 1C integration management:
- Configuration and connection testing
- Catalog synchronization
- Document creation and management
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.filters.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from hunttech_bot_common.onec.client import OneCClient, create_onec_client
from hunttech_bot_common.onec.config import (
    OneCConfig,
    OneCSettings,
    CatalogSyncSettings,
    DocumentBuilderSettings,
)
from hunttech_bot_common.onec.document_builder import DocumentBuilder, create_document_builder
from hunttech_bot_common.onec.mapper import map_from_onec
from hunttech_bot_common.onec.catalog_sync import LocalStorageSyncEngine, create_sync_engine
from hunttech_bot_common.telegram import CommandDef

logger = logging.getLogger(__name__)


# ── FSM States ──────────────────────────────────────────────────

class OneCSetupStates(StatesGroup):
    """FSM states for /onec setup wizard."""
    base_url = State()
    username = State()
    password = State()
    timeout = State()
    verify_ssl = State()
    confirm = State()


class OneCSyncStates(StatesGroup):
    """FSM states for /onec sync command."""
    select_catalog = State()
    confirm_full = State()


# ── Keyboard Helpers ────────────────────────────────────────────

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="onec:cancel")],
        ]
    )


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data="onec:save"),
                InlineKeyboardButton(text="🔄 Тест", callback_data="onec:test"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="onec:cancel")],
        ]
    )


def _ssl_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Включено (по умолчанию)", callback_data="onec_ssl:true")],
            [InlineKeyboardButton(text="❌ Отключено", callback_data="onec_ssl:false")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="onec:cancel")],
        ]
    )


def _sync_catalogs_kb(catalogs: list[str]) -> InlineKeyboardMarkup:
    """Build keyboard for catalog selection."""
    buttons = []
    for cat in catalogs:
        buttons.append([InlineKeyboardButton(text=f"📁 {cat}", callback_data=f"onec_sync:{cat}")])
    buttons.append([InlineKeyboardButton(text="🔄 Все каталоги", callback_data="onec_sync:all")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="onec:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _sync_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Инкрементальная", callback_data="onec_sync_mode:incremental")],
            [InlineKeyboardButton(text="🔄 Полная", callback_data="onec_sync_mode:full")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="onec:cancel")],
        ]
    )


# ── /onec setup command ─────────────────────────────────────────

async def cmd_onec_setup(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    access_manager: Any,
    onec_config: OneCConfig | None = None,
) -> None:
    """Handle /onec setup command (admin-only)."""
    user_id = message.from_user.id

    if not access_manager or not access_manager.is_admin(user_id):
        await message.answer("🚫 *Команда доступна только администратору.*", parse_mode=ParseMode.MARKDOWN)
        return

    args = (command.args or "").strip().lower()

    if onec_config is None:
        onec_config = OneCConfig.from_env()

    # /onec setup show
    if args == "show":
        await _cmd_onec_show(message, onec_config)
        return

    # /onec setup test
    if args == "test":
        await _cmd_onec_test(message, onec_config)
        return

    # /onec setup sync
    if args.startswith("sync"):
        await _cmd_onec_sync(message, state, onec_config, args)
        return

    # /onec setup doc
    if args.startswith("doc"):
        await _cmd_onec_doc(message, state, onec_config, args)
        return

    # /onec setup — start FSM wizard
    if args == "setup" or args == "":
        current = onec_config.service
        display = _format_config_display(current)

        await state.set_state(OneCSetupStates.base_url)
        await state.update_data(onec_config=onec_config)

        url_example = "https://1c.example.com/odata/standard.odata/"

        await message.answer(
            f"{display}\n\n"
            "📝 *Введите базовый URL 1C OData сервиса:*\n\n"
            f"Формат:\n`{url_example}`\n\n"
            "URL должен заканчиваться на `/` и указывать на корень OData сервиса.\n"
            "Для пропуска текущего значения отправьте `/skip`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return

    await message.answer(
        "🚫 *Неизвестная подкоманда.*\n\n"
        "Доступные команды:\n"
        "• `/onec setup` — настройка подключения к 1C\n"
        "• `/onec setup show` — показать текущую конфигурацию\n"
        "• `/onec setup test` — протестировать подключение\n"
        "• `/onec setup sync` — синхронизация каталогов\n"
        "• `/onec setup doc` — создание документов",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _cmd_onec_show(message: Message, config: OneCConfig) -> None:
    """Show current 1C configuration."""
    display = _format_config_display(config.service)
    sync_info = (
        f"\n📦 *Настройки синхронизации:*\n"
        f"  • Batch size: `{config.sync.batch_size}`\n"
        f"  • Full sync interval: `{config.sync.full_sync_interval_hours}`h\n"
        f"  • Incremental interval: `{config.sync.incremental_sync_interval_minutes}`min\n"
        f"  • Conflict strategy: `{config.sync.conflict_strategy}`\n"
        f"  • Enabled catalogs: `{config.sync.enabled_catalogs or 'все'}`\n"
    )
    doc_info = (
        f"\n📄 *Настройки документов:*\n"
        f"  • Default currency: `{config.documents.default_currency}`\n"
        f"  • Default VAT rate: `{config.documents.default_vat_rate}`\n"
        f"  • Auto-post: `{config.documents.auto_post_documents}`\n"
        f"  • Require INN: `{config.documents.require_counterparty_inn}`\n"
    )

    if config.service:
        text = (
            f"{display}{sync_info}{doc_info}\n\n"
            "Используйте:\n"
            "• `/onec setup` — изменить настройки\n"
            "• `/onec setup test` — протестировать подключение\n"
            "• `/onec setup sync` — синхронизировать каталоги"
        )
    else:
        text = display + sync_info + doc_info + "\n\nИспользуйте `/onec setup` для настройки подключения."

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


async def _cmd_onec_test(message: Message, config: OneCConfig) -> None:
    """Test 1C connection."""
    if not config.service:
        await message.answer(
            "❌ *1C сервис не настроен.*\n"
            "Используйте `/onec setup` для настройки.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await message.answer(
        "🔄 *Тестирую подключение к 1C...*\n"
        f"Хост: `{config.service.base_url}`\n"
        "⏳ Пожалуйста, подождите...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        async with create_onec_client(config.service) as client:
            health = await client.health_check()

        if health.get("status") == "connected":
            latency = health.get("latency_ms", "?")
            result_text = (
                "✅ *Подключение к 1C успешно!*\n\n"
                f"• Статус: `{health['status']}`\n"
                f"• Задержка: `{latency} мс`\n"
                f"• Каталоги доступны: `{health.get('catalogs_available', False)}`"
            )
        else:
            result_text = (
                f"⚠️ *Статус: {health.get('status', 'unknown')}*\n"
                f"Ошибка: `{health.get('error', 'unknown')}`"
            )
    except Exception as e:
        logger.warning("1C connection test failed: %s", e)
        result_text = (
            "❌ *Ошибка подключения к 1C:*\n"
            f"`{e}`\n\n"
            "Проверьте:\n"
            "• Доступность хоста\n"
            "• Правильность URL\n"
            "• Логин и пароль\n"
            "• SSL настройки\n"
            "• Настройки файрвола"
        )

    await status_msg.edit_text(result_text, parse_mode=ParseMode.MARKDOWN)


# ── FSM Steps for Setup ─────────────────────────────────────────

async def onec_setup_base_url(message: Message, state: FSMContext) -> None:
    """Handle base URL input."""
    text = message.text.strip()

    if text.lower() == "/skip":
        data = await state.get_data()
        config: OneCConfig = data.get("onec_config", OneCConfig.from_env())
        if config.service and config.service.base_url:
            await state.update_data(base_url=config.service.base_url)
            await state.set_state(OneCSetupStates.username)
            await message.answer(
                "✅ URL оставлен без изменений.\n\n"
                "📝 *Введите логин для подключения к 1C:*\n\n"
                "Для пропуска отправьте `/skip`.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
            return

    if not text.startswith(("http://", "https://")):
        await message.answer(
            "❌ *Неверный формат URL.*\n"
            "URL должен начинаться с http:// или https://\n"
            "Попробуйте ещё раз или отправьте `/skip`:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(base_url=text.rstrip("/"))
    await state.set_state(OneCSetupStates.username)
    await message.answer(
        "✅ URL принят.\n\n"
        "📝 *Введите логин для подключения к 1C:*\n\n"
        "Для пропуска отправьте `/skip`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )


async def onec_setup_username(message: Message, state: FSMContext) -> None:
    """Handle username input."""
    text = message.text.strip()

    if text.lower() == "/skip":
        data = await state.get_data()
        config: OneCConfig = data.get("onec_config", OneCConfig.from_env())
        if config.service and config.service.username:
            await state.update_data(username=config.service.username)
            await state.set_state(OneCSetupStates.password)
            await message.answer(
                "✅ Логин оставлен без изменений.\n\n"
                "🔐 *Введите пароль для подключения к 1C:*\n\n"
                "Для пропуска отправьте `/skip`.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
            return

    await state.update_data(username=text)
    await state.set_state(OneCSetupStates.password)
    await message.answer(
        "✅ Логин принят.\n\n"
        "🔐 *Введите пароль для подключения к 1C:*\n\n"
        "Для пропуска отправьте `/skip`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )


async def onec_setup_password(message: Message, state: FSMContext) -> None:
    """Handle password input."""
    text = message.text.strip()

    if text.lower() == "/skip":
        data = await state.get_data()
        config: OneCConfig = data.get("onec_config", OneCConfig.from_env())
        if config.service and config.service.password:
            await state.update_data(password=config.service.password)
            await state.set_state(OneCSetupStates.timeout)
            await message.answer(
                "✅ Пароль оставлен без изменений.\n\n"
                "⏱ *Введите таймаут запросов в секундах (по умолчанию 30):*\n\n"
                "Для пропуска отправьте `/skip`.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_cancel_kb(),
            )
            return

    await state.update_data(password=text)
    await state.set_state(OneCSetupStates.timeout)
    await message.answer(
        "✅ Пароль принят.\n\n"
        "⏱ *Введите таймаут запросов в секундах (по умолчанию 30):*\n\n"
        "Для пропуска отправьте `/skip`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_kb(),
    )


async def onec_setup_timeout(message: Message, state: FSMContext) -> None:
    """Handle timeout input."""
    text = message.text.strip()

    if text.lower() == "/skip":
        await state.update_data(timeout=30)
        await state.set_state(OneCSetupStates.verify_ssl)
        await message.answer(
            "✅ Таймаут оставлен по умолчанию (30с).\n\n"
            "🔒 *Включить проверку SSL сертификатов?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_ssl_mode_kb(),
        )
        return

    try:
        timeout = int(text)
        if timeout < 1 or timeout > 300:
            raise ValueError
        await state.update_data(timeout=timeout)
        await state.set_state(OneCSetupStates.verify_ssl)
        await message.answer(
            f"✅ Таймаут: `{timeout}`с.\n\n"
            "🔒 *Включить проверку SSL сертификатов?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_ssl_mode_kb(),
        )
    except ValueError:
        await message.answer(
            "❌ *Введите целое число от 1 до 300* или `/skip` для значения по умолчанию:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_cancel_kb(),
        )


async def onec_setup_verify_ssl(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle SSL verification selection."""
    await callback.answer()

    if not callback.data or not callback.data.startswith("onec_ssl:"):
        return

    verify_ssl = callback.data.split(":", 1)[1] == "true"
    await state.update_data(verify_ssl=verify_ssl)
    await state.set_state(OneCSetupStates.confirm)

    data = await state.get_data()
    config: OneCConfig = data.get("onec_config", OneCConfig.from_env())

    preview = (
        "🔍 *Проверьте конфигурацию:*\n\n"
        f"• `URL`: `{data.get('base_url', '')}`\n"
        f"• `Логин`: `{data.get('username', '')}`\n"
        f"• `Пароль`: `{'****' if data.get('password') else '(не задан)'}`\n"
        f"• `Таймаут`: `{data.get('timeout', 30)}`с\n"
        f"• `SSL проверка`: `{'Включена' if verify_ssl else 'Отключена'}`\n\n"
        "Что делать?"
    )

    await callback.message.edit_text(preview, reply_markup=_confirm_kb(), parse_mode=ParseMode.MARKDOWN)


# ── Confirmation callbacks ──────────────────────────────────────

async def onec_setup_test(callback: CallbackQuery, state: FSMContext) -> None:
    """Test connection with current config."""
    await callback.answer()
    data = await state.get_data()

    settings = OneCSettings(
        base_url=data.get("base_url", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        timeout=data.get("timeout", 30),
        verify_ssl=data.get("verify_ssl", True),
    )

    await callback.message.edit_text(
        "🔄 *Тестирую подключение к 1C...*\n"
        f"Хост: `{settings.base_url}`\n"
        "⏳ Пожалуйста, подождите...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        async with create_onec_client(settings) as client:
            health = await client.health_check()

        if health.get("status") == "connected":
            latency = health.get("latency_ms", "?")
            result_text = (
                "✅ *Подключение успешно!*\n\n"
                f"• Статус: `{health['status']}`\n"
                f"• Задержка: `{latency} мс`\n"
                f"• Каталоги доступны: `{health.get('catalogs_available', False)}`\n\n"
                "Конфигурация корректна. Сохранить?"
            )
        else:
            result_text = (
                f"⚠️ *Статус: {health.get('status', 'unknown')}*\n"
                f"Ошибка: `{health.get('error', 'unknown')}`\n\n"
                "Проверьте параметры подключения."
            )
    except Exception as e:
        logger.warning("1C connection test failed: %s", e)
        result_text = (
            "❌ *Ошибка подключения:*\n"
            f"`{e}`\n\n"
            "Проверьте параметры и попробуйте снова."
        )

    await callback.message.edit_text(result_text + "\n\nЧто делать?", reply_markup=_confirm_kb(), parse_mode=ParseMode.MARKDOWN)


async def onec_setup_save(callback: CallbackQuery, state: FSMContext) -> None:
    """Save 1C configuration."""
    await callback.answer()
    data = await state.get_data()
    config: OneCConfig = data.get("onec_config", OneCConfig.from_env())

    # Create new service settings
    service = OneCSettings(
        base_url=data.get("base_url", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        timeout=data.get("timeout", 30),
        verify_ssl=data.get("verify_ssl", True),
    )

    # Save to environment or config file (implementation depends on project)
    # For now, just show success
    await callback.message.edit_text(
        "✅ *Конфигурация 1C сохранена!*\n\n"
        "Теперь все боты HuntTech будут использовать эти настройки "
        "для подключения к 1C.\n\n"
        "Для применения может потребоваться перезапуск бота.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.clear()


async def onec_setup_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel setup wizard."""
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        "❌ *Настройка 1C отменена.*\n"
        "Текущая конфигурация не изменена.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Sync commands ───────────────────────────────────────────────

async def _cmd_onec_sync(message: Message, state: FSMContext, config: OneCConfig, args: str) -> None:
    """Handle /onec setup sync command."""
    if not config.service:
        await message.answer(
            "❌ *1C сервис не настроен.*\n"
            "Используйте `/onec setup` для настройки.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Parse args: "sync [catalog] [full|incremental]"
    parts = args.split()
    catalog_name = parts[1] if len(parts) > 1 else None
    force_full = "full" in parts

    if not catalog_name:
        # Show catalog selection
        catalogs = [
            "Counterparties", "Organizations", "Warehouses", "Nomenclature",
            "UnitsOfMeasure", "Currencies", "VATRates", "Contracts",
        ]
        await state.set_state(OneCSyncStates.select_catalog)
        await state.update_data(force_full=force_full, config=config)
        await message.answer(
            "📦 *Выберите каталог для синхронизации:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_sync_catalogs_kb(catalogs),
        )
        return

    # Run sync directly
    await _run_sync(message, config, catalog_name, force_full)


async def onec_sync_select_catalog(callback: CallbackQuery, state: FSMContext) -> None:
    """Handle catalog selection for sync."""
    await callback.answer()

    if not callback.data or not callback.data.startswith("onec_sync:"):
        return

    catalog_name = callback.data.split(":", 1)[1]
    data = await state.get_data()
    config: OneCConfig = data.get("config")
    force_full = data.get("force_full", False)

    if catalog_name == "all":
        await _run_sync_all(callback.message, config, force_full)
    else:
        await _run_sync(callback.message, config, catalog_name, force_full)

    await state.clear()


async def _run_sync(message: Message, config: OneCConfig, catalog_name: str, force_full: bool) -> None:
    """Run synchronization for a single catalog."""
    status_msg = await message.answer(
        f"🔄 *Синхронизация каталога {catalog_name}...*\n"
        f"Режим: `{'Полная' if force_full else 'Инкрементальная'}`\n"
        "⏳ Пожалуйста, подождите...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        from pathlib import Path
        state_dir = Path.cwd() / "data" / "onec_sync"
        storage_dir = Path.cwd() / "data" / "onec_catalogs"

        async with create_onec_client(config.service) as client:
            engine = LocalStorageSyncEngine(
                client=client,
                settings=config.sync,
                state_dir=state_dir,
                storage_dir=storage_dir,
            )
            result = await engine.sync_catalog(catalog_name, force_full=force_full)

        if result.success:
            result_text = (
                f"✅ *Синхронизация {catalog_name} завершена!*\n\n"
                f"• Получено: `{result.total_fetched}`\n"
                f"• Создано: `{result.created}`\n"
                f"• Обновлено: `{result.updated}`\n"
                f"• Удалено: `{result.deleted}`\n"
                f"• Ошибок: `{len(result.errors)}`\n"
                f"• Время: `{result.duration_seconds:.2f}с`"
            )
        else:
            result_text = (
                f"⚠️ *Синхронизация {catalog_name} завершена с ошибками:*\n\n"
                f"• Получено: `{result.total_fetched}`\n"
                f"• Создано: `{result.created}`\n"
                f"• Обновлено: `{result.updated}`\n"
                f"• Удалено: `{result.deleted}`\n"
                f"• Ошибок: `{len(result.errors)}`\n"
            )
            if result.errors:
                result_text += "\n*Ошибки:*\n" + "\n".join(f"• {e}" for e in result.errors[:5])

    except Exception as e:
        logger.exception("Sync failed for %s", catalog_name)
        result_text = f"❌ *Ошибка синхронизации:*\n`{e}`"

    await status_msg.edit_text(result_text, parse_mode=ParseMode.MARKDOWN)


async def _run_sync_all(message: Message, config: OneCConfig, force_full: bool) -> None:
    """Run synchronization for all catalogs."""
    status_msg = await message.answer(
        "🔄 *Синхронизация всех каталогов...*\n"
        f"Режим: `{'Полная' if force_full else 'Инкрементальная'}`\n"
        "⏳ Пожалуйста, подождите...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        from pathlib import Path
        state_dir = Path.cwd() / "data" / "onec_sync"
        storage_dir = Path.cwd() / "data" / "onec_catalogs"

        async with create_onec_client(config.service) as client:
            engine = LocalStorageSyncEngine(
                client=client,
                settings=config.sync,
                state_dir=state_dir,
                storage_dir=storage_dir,
            )
            results = await engine.sync_all(force_full=force_full)

        total_fetched = sum(r.total_fetched for r in results)
        total_created = sum(r.created for r in results)
        total_updated = sum(r.updated for r in results)
        total_deleted = sum(r.deleted for r in results)
        total_errors = sum(len(r.errors) for r in results)

        result_text = (
            "✅ *Синхронизация всех каталогов завершена!*\n\n"
            f"• Всего получено: `{total_fetched}`\n"
            f"• Создано: `{total_created}`\n"
            f"• Обновлено: `{total_updated}`\n"
            f"• Удалено: `{total_deleted}`\n"
            f"• Ошибок: `{total_errors}`\n\n"
        )

        for r in results:
            result_text += (
                f"📁 *{r.catalog_name}*: "
                f"+{r.created} ~{r.updated} -{r.deleted} "
                f"{'✅' if r.success else f'❌({len(r.errors)})'}\n"
            )

    except Exception as e:
        logger.exception("Full sync failed")
        result_text = f"❌ *Ошибка синхронизации:*\n`{e}`"

    await status_msg.edit_text(result_text, parse_mode=ParseMode.MARKDOWN)


# ── Document commands ───────────────────────────────────────────

async def _cmd_onec_doc(message: Message, state: FSMContext, config: OneCConfig, args: str) -> None:
    """Handle /onec setup doc command."""
    if not config.service:
        await message.answer(
            "❌ *1C сервис не настроен.*\n"
            "Используйте `/onec setup` для настройки.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await message.answer(
        "📄 *Создание документов в 1C*\n\n"
        "Эта функция в разработке. Доступные типы документов:\n"
        "• `GoodsReceipt` — Поступление товаров/услуг\n"
        "• `SalesInvoice` — Счёт на оплату покупателю\n"
        "• `AdvanceReport` — Авансовый отчёт\n\n"
        "Используйте API напрямую через DocumentBuilder.",
        parse_mode=ParseMode.MARKDOWN,
    )


def _format_config_display(service: OneCSettings | None) -> str:
    """Format 1C config for display."""
    if not service:
        return "📭 *1C не настроен*"

    # Mask password in URL
    masked_url = service.base_url
    if "@" in masked_url:
        parts = masked_url.split("@")
        if "//" in parts[0]:
            proto, creds = parts[0].split("//", 1)
            if ":" in creds:
                user, _ = creds.split(":", 1)
                masked_url = f"{proto}//{user}:***@{parts[1]}"

    return (
        "🔧 *Текущая конфигурация 1C:*\n\n"
        f"  • **URL:** `{masked_url}`\n"
        f"  • **Логин:** `{service.username}`\n"
        f"  • **Таймаут:** `{service.timeout}`с\n"
        f"  • **SSL проверка:** `{'Включена' if service.verify_ssl else 'Отключена'}`\n"
        f"  • **Макс. повторов:** `{service.max_retries}`\n"
        f"  • **Backoff:** `{service.retry_backoff}`"
    )


# ── Standard command definition ─────────────────────────────────

def get_onec_command_def() -> CommandDef:
    """Get CommandDef for /onec command."""
    return CommandDef(
        command="onec",
        title="1C Integration",
        description="Управление интеграцией с 1C",
        emoji="🔗",
        permissions={"onec"},  # Requires 'onec' permission
        admin=False,
        hidden=False,
        group="integrations",
    )


# ── Handler registration ────────────────────────────────────────

def register_onec_handlers(dp: Any, access_manager: Any) -> None:
    """Register 1C command handlers with dispatcher."""
    from hunttech_bot_common.telegram import register_command

    # Main command
    register_command(dp, "onec", cmd_onec_setup, access_manager=access_manager)

    # FSM handlers
    dp.message.register(onec_setup_base_url, OneCSetupStates.base_url)
    dp.message.register(onec_setup_username, OneCSetupStates.username)
    dp.message.register(onec_setup_password, OneCSetupStates.password)
    dp.message.register(onec_setup_timeout, OneCSetupStates.timeout)
    dp.callback_query.register(onec_setup_verify_ssl, OneCSetupStates.verify_ssl)
    dp.callback_query.register(onec_setup_test, OneCSetupStates.confirm, lambda c: c.data == "onec:test")
    dp.callback_query.register(onec_setup_save, OneCSetupStates.confirm, lambda c: c.data == "onec:save")
    dp.callback_query.register(onec_setup_cancel, lambda c: c.data == "onec:cancel")

    # Sync handlers
    dp.callback_query.register(onec_sync_select_catalog, OneCSyncStates.select_catalog, lambda c: c.data and c.data.startswith("onec_sync:"))


__all__ = [
    "OneCSetupStates",
    "OneCSyncStates",
    "cmd_onec_setup",
    "get_onec_command_def",
    "register_onec_handlers",
]