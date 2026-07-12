"""
Tests for Telegram BotCommand menu in HuntTechProtocols.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMenuRegistry:
    """Verify menu command registry is correctly defined."""

    def test_public_menu_commands_defined(self):
        """PUBLIC_MENU_COMMANDS exists and has entries."""
        from integrations.menu_adapter import PUBLIC_MENU_COMMANDS
        assert len(PUBLIC_MENU_COMMANDS) > 0
        # All entries should be (command, description) tuples
        for cmd, desc in PUBLIC_MENU_COMMANDS:
            assert isinstance(cmd, str)
            assert isinstance(desc, str)
            # Command should not start with /
            assert not cmd.startswith("/"), f"Command '{cmd}' must not start with /"

    def test_admin_menu_commands_defined(self):
        """ADMIN_MENU_COMMANDS exists."""
        from integrations.menu_adapter import ADMIN_MENU_COMMANDS
        assert isinstance(ADMIN_MENU_COMMANDS, list)
        for cmd, desc in ADMIN_MENU_COMMANDS:
            assert isinstance(cmd, str)
            assert not cmd.startswith("/")

    def test_all_menu_commands_have_handlers(self):
        """Every menu command should have a corresponding handler in bot.py."""
        from integrations.menu_adapter import PUBLIC_MENU_COMMANDS, ADMIN_MENU_COMMANDS

        # Read bot.py and find all Command decorators (including multi-line)
        bot_source = (Path(__file__).parent.parent / "bot.py").read_text("utf-8")

        # Find Command("name", "alias", ...) patterns — may span lines
        # Find Command("name", "alias", ...) patterns — may span lines
        handler_cmds = set()
        import re
        # First normalize: collapse Command( ... ) to a single line
        normalized = re.sub(r'Command\(([\s\S]*?)\)', lambda m: 'Command(' + m.group(1).replace('\n', ' ') + ')', bot_source)
        for m in re.finditer(r'Command\(([^)]+)\)', normalized):
            args = re.findall(r'"([^"]+)"', m.group(1))
            for arg in args:
                handler_cmds.add(arg)

        # Check each menu command has at least one corresponding handler
        all_menu = PUBLIC_MENU_COMMANDS + ADMIN_MENU_COMMANDS
        missing = [cmd for cmd, _ in all_menu if cmd not in handler_cmds]
        assert not missing, f"Commands without handlers: {missing}"

    def test_no_subcommands_as_menu_entries(self):
        """Menu must not contain commands with spaces (subcommands)."""
        from integrations.menu_adapter import PUBLIC_MENU_COMMANDS, ADMIN_MENU_COMMANDS
        all_menu = PUBLIC_MENU_COMMANDS + ADMIN_MENU_COMMANDS
        for cmd, desc in all_menu:
            assert " " not in cmd, f"Subcommand '{cmd}' must not be in menu"

    def test_description_length(self):
        """Descriptions must fit Telegram's limit (~256 chars)."""
        from integrations.menu_adapter import PUBLIC_MENU_COMMANDS, ADMIN_MENU_COMMANDS
        all_menu = PUBLIC_MENU_COMMANDS + ADMIN_MENU_COMMANDS
        for cmd, desc in all_menu:
            assert len(desc) <= 256, f"Description for '{cmd}' is too long: {len(desc)} chars"

    def test_menu_commands_not_exceeding_limit(self):
        """Total menu commands must not exceed Telegram's limit (100)."""
        from integrations.menu_adapter import PUBLIC_MENU_COMMANDS, ADMIN_MENU_COMMANDS
        total = len(PUBLIC_MENU_COMMANDS) + len(ADMIN_MENU_COMMANDS)
        assert total <= 100, f"Too many menu commands: {total}"

    def test_duplicate_commands(self):
        """No duplicate commands in menu."""
        from integrations.menu_adapter import PUBLIC_MENU_COMMANDS, ADMIN_MENU_COMMANDS
        all_cmds = [cmd for cmd, _ in PUBLIC_MENU_COMMANDS + ADMIN_MENU_COMMANDS]
        assert len(all_cmds) == len(set(all_cmds)), "Duplicate commands in menu"


class TestMenuBuild:
    """Verify menu building logic."""

    def test_build_menu_default(self):
        """_build_menu returns default commands for non-admin."""
        from integrations.menu_adapter import _build_menu
        cmds = _build_menu(is_admin=False)
        # Should not contain admin commands
        admin_cmd_names = {cmd for cmd, _ in cmds}
        assert "setup_db" not in admin_cmd_names or False  # setup_db is admin-only

    def test_build_menu_admin(self):
        """_build_menu returns admin commands for admin."""
        from integrations.menu_adapter import _build_menu, PUBLIC_MENU_COMMANDS, ADMIN_MENU_COMMANDS
        cmds = _build_menu(is_admin=True)
        admin_cmd_names = {cmd for cmd, _ in cmds}
        if ADMIN_MENU_COMMANDS:
            admin_first = ADMIN_MENU_COMMANDS[0][0]
            assert admin_first in admin_cmd_names

    def test_menu_hash_stable(self):
        """Same menu produces same hash."""
        from integrations.menu_adapter import _hash
        cmds1 = [("start", "Start"), ("help", "Help")]
        cmds2 = [("start", "Start"), ("help", "Help")]
        assert _hash(cmds1) == _hash(cmds2)

    def test_menu_hash_changes(self):
        """Different menu produces different hash."""
        from integrations.menu_adapter import _hash
        cmds1 = [("start", "Start")]
        cmds2 = [("start", "Start different")]
        assert _hash(cmds1) != _hash(cmds2)


class TestMenuSync:
    """Verify menu synchronization logic (mocked)."""

    @pytest.mark.asyncio
    async def test_sync_menu_calls_set_my_commands(self):
        """_sync_menu calls bot.set_my_commands with correct scope."""
        from integrations.menu_adapter import _sync_menu, invalidate_menu
        mock_bot = MagicMock()
        mock_bot.set_my_commands = AsyncMock()

        invalidate_menu(123)
        result = await _sync_menu(mock_bot, 123)

        assert result is True
        mock_bot.set_my_commands.assert_called_once()
        call_kwargs = mock_bot.set_my_commands.call_args[1]
        assert "commands" in call_kwargs
        assert "scope" in call_kwargs
        # Scope should be BotCommandScopeChat
        scope = call_kwargs["scope"]
        assert scope.type == "chat"

    @pytest.mark.asyncio
    async def test_sync_menu_skips_duplicate(self):
        """_sync_menu does NOT call API if hash unchanged."""
        from integrations.menu_adapter import _sync_menu, _menu_hashes
        mock_bot = MagicMock()
        mock_bot.set_my_commands = AsyncMock()

        # First call should trigger API
        await _sync_menu(mock_bot, 456)
        assert mock_bot.set_my_commands.call_count == 1

        # Second call with same user should skip (hash match)
        mock_bot.set_my_commands.reset_mock()
        await _sync_menu(mock_bot, 456)
        assert mock_bot.set_my_commands.call_count == 0

    @pytest.mark.asyncio
    async def test_sync_menu_api_error_logged(self):
        """_sync_menu handles API errors gracefully."""
        from integrations.menu_adapter import _sync_menu, invalidate_menu
        mock_bot = MagicMock()
        mock_bot.set_my_commands = AsyncMock(side_effect=Exception("API error"))

        invalidate_menu(789)
        result = await _sync_menu(mock_bot, 789)

        assert result is False  # Graceful failure

    @pytest.mark.asyncio
    async def test_sync_default_menu(self):
        """_sync_default_menu sets default scope."""
        from integrations.menu_adapter import _sync_default_menu
        mock_bot = MagicMock()
        mock_bot.set_my_commands = AsyncMock()

        await _sync_default_menu(mock_bot)

        mock_bot.set_my_commands.assert_called_once()
        call_kwargs = mock_bot.set_my_commands.call_args[1]
        assert call_kwargs.get("scope") is None  # default scope

    @pytest.mark.asyncio
    async def test_sync_admin_menu(self):
        """_sync_admin_menu sets admin scope."""
        from integrations.menu_adapter import _sync_admin_menu
        mock_bot = MagicMock()
        mock_bot.set_my_commands = AsyncMock()

        await _sync_admin_menu(mock_bot)

        mock_bot.set_my_commands.assert_called_once()
        call_kwargs = mock_bot.set_my_commands.call_args[1]
        scope = call_kwargs["scope"]
        assert scope.type == "chat"

    def test_invalidate_menu_removes_hash(self):
        """invalidate_menu clears cached hash for user."""
        from integrations.menu_adapter import invalidate_menu, _menu_hashes
        _menu_hashes["123"] = "somehash"
        invalidate_menu(123)
        assert "123" not in _menu_hashes


class TestIsAdmin:
    """Verify admin check logic."""

    def test_admin_user(self):
        """is_admin returns True for admin ID."""
        from integrations.menu_adapter import is_admin, ADMIN_USER_ID
        assert is_admin(ADMIN_USER_ID) is True

    def test_non_admin_user(self):
        """is_admin returns False for non-admin ID."""
        from integrations.menu_adapter import is_admin
        assert is_admin(999999) is False
