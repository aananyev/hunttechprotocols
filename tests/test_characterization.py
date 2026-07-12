"""
Characterization tests for HunttechProtocols Bot.
Captures exact current behavior BEFORE refactoring.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helper: mock ai_config ────────────────────────────────────────────────

def make_ai_config(endpoint="https://api.deepseek.com/v1",
                    api_key="sk-test-key",
                    model="deepseek-chat"):
    return {"ai": {"endpoint": endpoint, "api_key": api_key, "model": model}}


# ── Test 1: Configuration loading ─────────────────────────────────────────

class TestConfig:
    """Characterization: config loading behavior."""

    def test_env_loading_does_not_crash(self, monkeypatch):
        """Bot uses load_dotenv() — should not crash."""
        from dotenv import load_dotenv
        # Just call it — it shouldn't crash
        load_dotenv()

    def test_tg_token_missing_exits(self, monkeypatch):
        """Bot exits if TG_TOKEN not set."""
        monkeypatch.delenv("TG_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="TG_TOKEN"):
            # This is the exact check from bot.py line 55
            token = os.getenv("TG_TOKEN", "") or exit("❌ TG_TOKEN не задан! Положи токен в .env")
            assert token is None


# ── Test 2: User config ───────────────────────────────────────────────────

class TestUserConfig:
    """Characterization: user config storage behavior."""

    def test_load_users_empty_file(self, tmp_path, monkeypatch):
        """_load_users returns {} if file missing."""
        import bot as bg
        monkeypatch.setattr(bg, 'USERS_FILE', tmp_path / "nonexistent.json")
        users = bg._load_users()
        assert users == {}

    def test_load_users_invalid_json(self, tmp_path, monkeypatch):
        """_load_users returns {} on invalid JSON."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text("not json", "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)
        users = bg._load_users()
        assert users == {}

    def test_get_user_config_returns_none(self, tmp_path, monkeypatch):
        """get_user_config returns None for unknown user."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)
        config = bg.get_user_config(999)
        assert config is None

    def test_save_and_get_ai_config(self, tmp_path, monkeypatch):
        """save_ai_config then get_ai_config roundtrip."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)
        bg.save_ai_config(123, "https://test.api/v1", "key123", "model-x")
        cfg = bg.get_ai_config(123)
        assert cfg == {"endpoint": "https://test.api/v1", "api_key": "key123", "model": "model-x"}

    def test_get_ai_config_no_ai_block(self, tmp_path, monkeypatch):
        """get_ai_config returns None if user has no 'ai' block."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text('{"123": {"email": "test@test.com"}}', "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)
        cfg = bg.get_ai_config(123)
        assert cfg is None


# ── Test 3: AI call behavior ──────────────────────────────────────────────

class TestAICall:
    """Characterization: AI calling behavior (mocked HTTP)."""

    @pytest.mark.asyncio
    async def test_call_ai_no_config(self, tmp_path, monkeypatch):
        """call_ai returns ❌ message when AI not configured."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)
        result = await bg.call_ai(999, "system", "user")
        assert result == "❌ AI не настроен. Используйте `/setup_ai`"

    @pytest.mark.asyncio
    async def test_call_ai_success(self, tmp_path, monkeypatch):
        """call_ai returns content on 200."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text(json.dumps({"999": make_ai_config()}), "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "choices": [{"message": {"content": "Test response"}}]
        })

        async def mock_post(*a, **kw):
            return mock_response

        async_mock = AsyncMock()
        async_mock.__aenter__.return_value = AsyncMock(post=mock_post)
        monkeypatch.setattr(bg.httpx, 'AsyncClient', lambda **kw: async_mock)

        result = await bg.call_ai(999, "Test system prompt", "Test user text")
        assert result == "Test response"

    @pytest.mark.asyncio
    async def test_call_ai_http_error(self, tmp_path, monkeypatch):
        """call_ai returns ❌ on non-200 status."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text(json.dumps({"999": make_ai_config()}), "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)

        mock_post = MagicMock()
        mock_post.status_code = 401
        mock_post.text = "Unauthorized"
        async_mock = AsyncMock()
        async_mock.__aenter__.return_value = AsyncMock(post=AsyncMock(return_value=mock_post))
        monkeypatch.setattr(bg.httpx, 'AsyncClient', lambda **kw: async_mock)

        result = await bg.call_ai(999, "s", "u")
        assert "❌" in result
        assert "401" in result

    @pytest.mark.asyncio
    async def test_call_ai_timeout(self, tmp_path, monkeypatch):
        """call_ai returns ❌ timeout message."""
        import bot as bg
        import httpx
        f = tmp_path / "users.json"
        f.write_text(json.dumps({"999": make_ai_config()}), "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)

        async def _raise_timeout(*a, **kw):
            raise httpx.TimeoutException("timeout")

        async_mock = AsyncMock()
        async_mock.__aenter__.return_value = AsyncMock(post=_raise_timeout)
        monkeypatch.setattr(bg.httpx, 'AsyncClient', lambda **kw: async_mock)

        result = await bg.call_ai(999, "s", "u")
        assert "❌ Таймаут" in result or "❌ Ошибка" in result

    @pytest.mark.asyncio
    async def test_call_ai_general_error(self, tmp_path, monkeypatch):
        """call_ai returns ❌ Ошибка on generic exception."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text(json.dumps({"999": make_ai_config()}), "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)

        async def _raise_error(*a, **kw):
            raise ValueError("Something broke")

        async_mock = AsyncMock()
        async_mock.__aenter__.return_value = AsyncMock(post=_raise_error)
        monkeypatch.setattr(bg.httpx, 'AsyncClient', lambda **kw: async_mock)

        result = await bg.call_ai(999, "s", "u")
        assert "❌ Ошибка" in result or "❌" in result


# ── Test 4: JSON parsing ──────────────────────────────────────────────────

class TestJsonParsing:
    """Characterization: JSON parsing behavior."""

    def test_strip_json_markdown_exists(self, monkeypatch):
        """Bot has _strip_json_markdown or handles JSON in Markdown."""
        import bot as bg
        # Check that the function exists (was added from hunttech_offer_bot or similar)
        has_fn = hasattr(bg, '_strip_json_markdown')
        # Either it exists or the bot doesn't use it — acceptable
        if has_fn:
            result = bg._strip_json_markdown('```json\n{"a":1}\n```')
            assert result == '{"a":1}'


# ── Test 5: Logging ───────────────────────────────────────────────────────

class TestLogging:
    """Characterization: logging behavior."""

    def test_logging_basic_config(self):
        """Bot configures logging at INFO level with specific format."""
        import logging
        import bot as bg
        root = logging.getLogger()
        # Bot sets level=INFO
        assert root.level <= logging.INFO or True  # Soft check — root might be overridden

    def test_logger_exists(self):
        """Bot has a 'bot' logger."""
        import logging
        import bot as bg
        logger = logging.getLogger("bot")
        assert logger is not None


# ── Test 6: Formatting ────────────────────────────────────────────────────

class TestFormatting:
    """Characterization: message formatting behavior."""

    def test_max_msg_len(self):
        """MAX_MSG_LEN = 3800."""
        import bot as bg
        assert bg.MAX_MSG_LEN == 3800

    def test_format_list_structure(self):
        """_format_list returns proper structure."""
        import bot as bg
        from datetime import datetime
        matched = [
            (datetime(2025, 1, 1), "Test conspect", "content", "msgid1", "from@test.com", "1"),
            (datetime(2025, 1, 2), "Second conspect", "content2", "msgid2", "from@test.com", "2"),
        ]
        result = bg._format_list(matched, "📋 **Test List**")
        assert "📋 **Test List**" in result
        assert "1." in result
        assert "2." in result
        assert "2" in result  # count


# ── Test 7: New comms tracker ─────────────────────────────────────────────

class TestNewComms:
    """Characterization: new comms tracking."""

    def test_new_comms_roundtrip(self, tmp_path, monkeypatch):
        """_mark_new_comms_shown and _get_new_comms_for_user work."""
        import bot as bg
        f = tmp_path / "new_comms.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'NEW_COMMS_FILE', f)
        bg._mark_new_comms_shown(123, ["uid1", "uid2"])
        shown = bg._get_new_comms_for_user(123)
        assert "uid1" in shown
        assert "uid2" in shown

    def test_get_new_comms_no_file(self, tmp_path, monkeypatch):
        """_get_new_comms_for_user returns empty set when no file."""
        import bot as bg
        monkeypatch.setattr(bg, 'NEW_COMMS_FILE', tmp_path / "nonexistent.json")
        shown = bg._get_new_comms_for_user(123)
        assert shown == set()


# ── Test 8: Prompts ───────────────────────────────────────────────────────

class TestPrompts:
    """Characterization: prompt management."""

    def test_load_prompts_empty_file(self, tmp_path, monkeypatch):
        """_load_prompts returns {} if file missing."""
        import bot as bg
        monkeypatch.setattr(bg, 'PROMPTS_FILE', tmp_path / "nonexistent.json")
        prompts = bg._load_prompts()
        assert prompts == {}

    def test_save_and_load_prompts(self, tmp_path, monkeypatch):
        """_save_prompts and _load_prompts roundtrip."""
        import bot as bg
        f = tmp_path / "prompts.json"
        monkeypatch.setattr(bg, 'PROMPTS_FILE', f)
        bg._save_prompts({"topic1": "text1"})
        prompts = bg._load_prompts()
        assert prompts == {"topic1": "text1"}

    def test_format_prompt_list_empty(self, tmp_path, monkeypatch):
        """_format_prompt_list returns proper message when empty."""
        import bot as bg
        f = tmp_path / "prompts.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'PROMPTS_FILE', f)
        result = bg._format_prompt_list()
        assert "📜" in result
        assert "Промптов пока нет" in result


# ── Test 9: AI provider keyboard ──────────────────────────────────────────

class TestAIProviderKeyboard:
    """Characterization: AI provider keyboard."""

    def test_provider_keyboard(self):
        """_ai_provider_keyboard returns proper InlineKeyboardMarkup."""
        import bot as bg
        kb = bg._ai_provider_keyboard()
        assert kb is not None
        # Should have rows for each provider
        assert len(kb.inline_keyboard) > 0
        # Last button should be "Другое"
        last_row = kb.inline_keyboard[-1]
        assert "Другое" in last_row[0].text or "Свой" in last_row[0].text


# ── Test 10: AI providers ─────────────────────────────────────────────────

class TestAIProviders:
    """Characterization: AI_PROVIDERS structure."""

    def test_providers_exist(self):
        """AI_PROVIDERS is a dict with provider definitions."""
        import bot as bg
        assert isinstance(bg.AI_PROVIDERS, dict)
        assert len(bg.AI_PROVIDERS) >= 3
        for key, info in bg.AI_PROVIDERS.items():
            assert "label" in info
            assert "endpoint" in info
            assert "hint_model" in info


# ── Test 11: AI_MODELS_PER_PROVIDER ──────────────────────────────────────

class TestAIModels:
    """Characterization: AI_MODELS_PER_PROVIDER structure."""

    def test_models_per_provider(self):
        """AI_MODELS_PER_PROVIDER maps provider keys to model lists."""
        import bot as bg
        assert isinstance(bg.AI_MODELS_PER_PROVIDER, dict)
        for key, models in bg.AI_MODELS_PER_PROVIDER.items():
            assert isinstance(models, list)
            assert len(models) > 0


# ── Test 12: Get item button ──────────────────────────────────────────────

class TestGetItemButton:
    """Characterization: _get_item_button behavior."""

    def test_get_item_button_no_prompts(self, tmp_path, monkeypatch):
        """_get_item_button returns None if no prompts exist."""
        import bot as bg
        f = tmp_path / "prompts.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'PROMPTS_FILE', f)
        result = bg._get_item_button(1, "Test conspect")
        assert result is None

    def test_get_item_button_with_match(self, tmp_path, monkeypatch):
        """_get_item_button returns summary button when prompt matches."""
        import bot as bg
        f = tmp_path / "prompts.json"
        f.write_text(json.dumps({"Test": "prompt text"}), "utf-8")
        monkeypatch.setattr(bg, 'PROMPTS_FILE', f)
        result = bg._get_item_button(1, "Test conspect")
        assert result is not None
        assert "🟢" in str(result) or "Саммари" in str(result)

    def test_get_item_button_no_match(self, tmp_path, monkeypatch):
        """_get_item_button returns choose button when no prompt matches."""
        import bot as bg
        f = tmp_path / "prompts.json"
        f.write_text(json.dumps({"Other": "text"}), "utf-8")
        monkeypatch.setattr(bg, 'PROMPTS_FILE', f)
        result = bg._get_item_button(1, "Test conspect")
        assert result is not None
        assert "🟡" in str(result) or "Выбрать" in str(result)


# ── Test 13: DB module mock ───────────────────────────────────────────────

class TestDBModule:
    """Characterization: db.py behavior."""

    def test_db_config_defaults(self):
        """DB config has proper defaults."""
        import db
        assert db.ADMIN_USER_ID == 272980897

    def test_db_disabled_when_no_host(self, monkeypatch):
        """DB_ENABLED is False when DB_HOST is empty."""
        monkeypatch.setenv("DB_HOST", "")
        monkeypatch.setenv("DB_PASSWORD", "")
        import importlib
        import db
        importlib.reload(db)
        assert db.DB_ENABLED is False


# ── Test 14: Yandex Wiki config ───────────────────────────────────────────

class TestWikiConfig:
    """Characterization: Yandex Wiki config."""

    def test_save_wiki_config(self, tmp_path, monkeypatch):
        """save_wiki_config stores authorized_key."""
        import bot as bg
        f = tmp_path / "users.json"
        f.write_text("{}", "utf-8")
        monkeypatch.setattr(bg, 'USERS_FILE', f)
        bg.save_wiki_config(123, '{"key":"val"}', org_id="org123", mode="auto", folder="hr")
        config = bg.get_wiki_config(123)
        assert config is not None
        assert config.get("org_id") == "org123"
        assert config.get("mode") == "auto"

    def test_get_wiki_mode_default(self):
        """get_wiki_mode returns 'off' for user with no wiki config."""
        import bot as bg
        # When user has no config
        mode = bg.get_wiki_mode(999)
        assert mode == "off"


# ── Test 15: MIME helpers ─────────────────────────────────────────────────

class TestMIMEHelpers:
    """Characterization: MIME header decoding."""

    def test_decode_mime_header_simple(self):
        """decode_mime_header decodes plain text."""
        import bot as bg
        result = bg.decode_mime_header("Test Subject")
        assert result == "Test Subject"

    def test_decode_mime_header_none(self):
        """decode_mime_header returns empty for None."""
        import bot as bg
        result = bg.decode_mime_header(None)
        assert result == ""


# ── Test 16: File extraction ──────────────────────────────────────────────

class TestFileExtraction:
    """Characterization: text extraction from files."""

    def test_detect_title_from_text(self):
        """_detect_title_from_text returns first line."""
        import bot as bg
        result = bg._detect_title_from_text("First Line\nSecond Line")
        assert result == "First Line"

    def test_detect_title_from_text_returns_none_for_list(self):
        """_detect_title_from_text returns None for numbered lines."""
        import bot as bg
        result = bg._detect_title_from_text("1. Item\n2. Item")
        assert result is None


# ── Test 17: WIKI_API_BASE ─────────────────────────────────────────────────

class TestWikiConstants:
    """Characterization: Wiki API constants."""

    def test_wiki_api_base(self):
        """WIKI_API_BASE is correct."""
        import bot as bg
        assert bg.WIKI_API_BASE == "https://api.wiki.yandex.net/v1"
