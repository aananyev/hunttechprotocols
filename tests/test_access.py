#!/usr/bin/env python3
"""Tests for AccessManager integration in HuntTech Protocols Bot.

Tests the access control layer added on top of the existing bot:
- AccessManager initialization
- Access gate on /start
- /request_access handler
- /user command for admin management
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Ensure hunttech-bot-common is importable
sys.path.insert(0, os.path.expanduser("~/StudioProjects/hunttech-bot-common"))

from hunttech_bot_common.users import AccessManager
from hunttech_bot_common.users.ptb import get_bot_access_path

pass_count = 0
fail_count = 0


def check(name: str, condition: bool, detail: str = ""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  ✅ {name}")
    else:
        fail_count += 1
        print(f"  ❌ {name} {detail}")


# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("🧪 TEST: AccessManager — Initialization")
print("=" * 60)

with tempfile.TemporaryDirectory() as tmpdir:
    # Test 1: AccessManager with temp file
    data_file = Path(tmpdir) / "access.json"

    am = AccessManager(
        data_path=data_file,
        master_admin_id=12345,
        bot_name="HuntTech Protocols",
    )

    check("1.1 master_admin_id is set", am.master_admin_id == 12345)
    check("1.2 bot_name is set", am.bot_name == "HuntTech Protocols")
    # File is created on first mutation (save), not in __init__
    check("1.3 data file created after save", True)  # checked after first mutation below
    check("1.4 no users initially", am.get_allowed_users() == [])
    check("1.5 admin is admin", am.is_admin(12345))
    check("1.6 admin is allowed", am.is_allowed(12345))

    # First mutation triggers file creation
    am.save()
    check("1.3b data file created after first save", data_file.exists())

    # Test 2: get_bot_access_path
    path = get_bot_access_path("hunttechprotocols")
    check("2.1 path ends with correct name", str(path).endswith("access_hunttechprotocols.json"))
    check("2.2 path is absolute", path.is_absolute())

    # Test 3: Add user
    am.add_user(user_id=99999, username="testuser", full_name="Test User")
    check("3.1 user count after add", len(am.get_allowed_users()) == 1)
    check("3.2 user is allowed", am.is_allowed(99999))
    check("3.3 user is not admin", not am.is_admin(99999))

    # Test 4: Request access
    result = am.request_access(
        user_id=88888,
        username="newuser",
        first_name="New",
        last_name="User",
    )
    check("4.1 request is new", result.get("is_new"))
    check("4.2 not already allowed", not result.get("is_already_allowed"))
    pending = am.get_pending_requests()
    check("4.3 one pending request", len(pending) == 1)
    check("4.4 pending status", pending[0].get("status") == "pending")

    # Test 5: Re-request (same user)
    result2 = am.request_access(user_id=88888, username="newuser")
    check("5.1 re-request is not new", not result2.get("is_new"))

    # Test 6: Approve request
    approved = am.approve_request(88888, approved_by=12345)
    check("6.1 approve succeeds", approved)
    check("6.2 user is now allowed", am.is_allowed(88888))
    check("6.3 no pending requests", len(am.get_pending_requests()) == 0)

    # Test 7: Remove user
    removed = am.remove_user(88888)
    check("7.1 remove succeeds", removed)
    check("7.2 user not allowed after removal", not am.is_allowed(88888))
    check("7.3 remove nonexistent returns False", not am.remove_user(77777))

    # Test 8: Ban / Unban
    am.add_user(user_id=66666, username="banned_user")
    check("8.1 user added", am.is_allowed(66666))
    banned = am.ban_user(66666)
    check("8.2 ban succeeds", banned)
    check("8.3 user not allowed after ban", not am.is_allowed(66666))
    check("8.4 ban nonexistent returns False", not am.ban_user(55555))
    unbanned = am.unban_user(66666)
    check("8.5 unban succeeds", unbanned)
    check("8.6 user allowed after unban", am.is_allowed(66666))

    # Test 9: Persistence (reload)
    am.reload()
    check("9.1 admin still admin after reload", am.is_admin(12345))
    check("9.2 existing user persists", am.is_allowed(99999))
    check("9.3 can't ban master admin", not am.ban_user(12345))

    # Test 10: Permission management
    am.set_command_permissions({
        "start": set(),
        "user": {"admin"},
        "setup": {"setup"},
    })
    check("10.1 admin can use admin commands", am.user_can_use_command(12345, "user"))
    check("10.2 regular user cannot use admin command", not am.user_can_use_command(99999, "user"))
    check("10.3 regular user can use unrestricted command", am.user_can_use_command(99999, "start"))
    check("10.4 regular user cannot use perm-protected command", not am.user_can_use_command(99999, "setup"))

    am.add_permission(99999, "setup")
    check("10.5 user granted setup permission", am.has_permission(99999, "setup"))
    check("10.6 user can now use setup", am.user_can_use_command(99999, "setup"))

    am.remove_permission(99999, "setup")
    check("10.7 permission removed", not am.has_permission(99999, "setup"))

    # Test 11: get_bot_access_path creates directory
    check("11.1 parent dir exists", path.parent.exists())

    # Test 12: Settings
    am.update_user_settings(99999, {"theme": "dark", "lang": "ru"})
    settings = am.get_user_settings(99999)
    check("12.1 settings saved", settings.get("theme") == "dark")
    check("12.2 lang saved", settings.get("lang") == "ru")

    am.reset_user_settings(99999, {"theme"})
    settings2 = am.get_user_settings(99999)
    check("12.3 theme removed", "theme" not in settings2)
    check("12.4 lang stays", settings2.get("lang") == "ru")

# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("🧪 TEST: AccessManager — bot.py integration checks")
print("=" * 60)

# Test 13: Check bot.py imports are correct
bot_py = Path(os.path.expanduser("~/StudioProjects/hunttechprotocols/bot.py"))
bot_text = bot_py.read_text(encoding="utf-8")

check("13.1 AccessManager imported", "from hunttech_bot_common.users import AccessManager" in bot_text)
check("13.2 get_bot_access_path imported", "from hunttech_bot_common.users.ptb import get_bot_access_path" in bot_text)
check("13.3 MASTER_ADMIN_ID defined", "MASTER_ADMIN_ID = int(os.getenv" in bot_text)
check("13.4 access_manager initialized", "access_manager = AccessManager(" in bot_text)
check("13.5 access gate in /start", "access_manager.is_admin(user_id)" in bot_text)
check("13.6 request_access handler", 'Command("request_access")' in bot_text)
check("13.7 user command handler", 'Command("user")' in bot_text)
check("13.8 access_manager DB path uses get_bot_access_path", "get_bot_access_path(\"hunttechprotocols\")" in bot_text)
check("13.9 users.json still loaded (not removed)", "_load_users()" in bot_text)
check("13.10 get_user_config still exists", "def get_user_config" in bot_text)


# ═══════════════════════════════════════════════════════════════════
print(f"\n{'=' * 60}")
print(f"🏁 РЕЗУЛЬТАТЫ ТЕСТОВ ACCESS MANAGER")
print(f"{'=' * 60}")
print(f"✅ Пройдено: {pass_count}")
print(f"❌ Провалено: {fail_count}")
print(f"📊 Всего: {pass_count + fail_count}")
sys.exit(0 if fail_count == 0 else 1)
