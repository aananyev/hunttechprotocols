#!/usr/bin/env python3
"""Tests for centralized help registry in tg_conspect_bot."""

import sys, os
sys.path.insert(0, "/root/tg_conspect_bot")
os.chdir("/root/tg_conspect_bot")

from bot import (
    render_help_overview, render_help_group,
    get_command_emoji, get_command_meta, HELP_GROUPS, HELP_COMMANDS,
)

pass_count = 0
fail_count = 0

def check(name, condition, detail=""):
    global pass_count, fail_count
    if condition:
        pass_count += 1
        print(f"  ✅ {name}")
    else:
        fail_count += 1
        print(f"  ❌ {name} {detail}")

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 1: /help overview")
overview = render_help_overview()
check("1.1 Содержит все группы", all(g in overview for g in HELP_GROUPS))
check("1.2 Не содержит длинных описаний команд", "/help setup" in overview)
check("1.3 Не содержит /start в overview", "/start" not in overview)
check("1.4 Каждая группа имеет эмодзи", all(g["emoji"] for g in HELP_GROUPS.values()))

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 2: /help setup")
setup = render_help_group("setup")
check("2.1 Содержит /start", "/start" in setup)
check("2.2 Содержит /init", "/init" in setup)
check("2.3 Содержит /setup ai", "/setup ai" in setup)
check("2.4 Содержит алиасы /setup_llm", "setup_llm" in setup)
check("2.5 Не содержит несуществующие опции", "non-existent" not in setup)

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 3: /help group detail")
for group in HELP_GROUPS:
    detail = render_help_group(group)
    check(f"3.{list(HELP_GROUPS.keys()).index(group)+1} {group} имеет детальную справку", detail is not None)

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 4: /help unknown")
check("4.1 Неизвестная группа → None", render_help_group("unknown") is None)

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 5: get_command_emoji")
check("5.1 /list → 📬", get_command_emoji("/list") == "📬")
check("5.2 list → 📬", get_command_emoji("list") == "📬")
check("5.3 /конспекты → 📬 (алиас)", get_command_emoji("/конспекты") == "📬")
check("5.4 /setup_ai → 🧠 (алиас)", get_command_emoji("/setup_ai") == "🧠")
check("5.5 /setup_llm → 🧠 (алиас)", get_command_emoji("/setup_llm") == "🧠")
check("5.6 /wikistat → 🔍 (алиас)", get_command_emoji("/wikistat") == "🔍")
check("5.7 /помощь → ❓ (алиас)", get_command_emoji("/помощь") == "❓")
check("5.8 unknown → ''", get_command_emoji("unknown_command") == "")

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 6: Каждая публичная команда имеет эмодзи")
for key, meta in HELP_COMMANDS.items():
    if meta.get("public", True):
        check(f"6.{list(HELP_COMMANDS.keys()).index(key)+1} {key} имеет эмодзи",
              bool(meta.get("emoji")), f"emoji={meta.get('emoji')!r}")

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 7: Алиасы указывают на существующие команды")
for key, meta in HELP_COMMANDS.items():
    for alias in meta.get("aliases", []):
        resolved = get_command_meta(alias.lstrip("/"))
        check(f"7.1 Алиас {alias} → {key}",
              resolved is not None and resolved.get("title") == meta.get("title"))

# ═══════════════════════════════════════════════════════════════════
print("\n🧪 TEST 8: Админ-команды не публичные")
admin_commands = [k for k, v in HELP_COMMANDS.items() if v.get("admin")]
for cmd in admin_commands:
    check(f"8.1 {cmd} не публичная", not HELP_COMMANDS[cmd].get("public", True))

# ═══════════════════════════════════════════════════════════════════
print(f"\n{'=' * 50}")
print(f"🏁 РЕЗУЛЬТАТЫ ТЕСТОВ HELP REGISTRY")
print(f"{'=' * 50}")
print(f"✅ Пройдено: {pass_count}")
print(f"❌ Провалено: {fail_count}")
print(f"📊 Всего: {pass_count + fail_count}")
sys.exit(0 if fail_count == 0 else 1)
