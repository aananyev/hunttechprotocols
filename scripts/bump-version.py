#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Инкремент версии бота в pyproject.toml при каждом коммите.

Формат версии: `version = "X.Y.Z"` (допустимо и X.Y).
По умолчанию инкрементится ПОСЛЕДНИЙ сегмент (подверсия):
    1.0.0 -> 1.0.1
    0.1   -> 0.2
Опциональные аргументы:
    patch  — последний сегмент (по умолчанию)
    minor  — средний сегмент, младшие обнуляются
    major  — первый сегмент, остальные обнуляются
    --dry-run — только показать результат, файл не менять

Возвращает 0 при успехе, 1 если строка версии не найдена.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

# `version = "1.0.0"` — строка в pyproject.toml.
# \bversion\b не задевает project.version-like ключи без границы слова.
VERSION_LINE = re.compile(
    r"^(.*\bversion\b[ \t]*=[ \t]*)(['\"])(\d+)(?:\.(\d+))?(?:\.(\d+))?(['\"])(.*)$",
    re.M,
)


def bump(parts, mode):
    parts = list(parts)
    if mode == "major":
        parts[0] += 1
        parts[1:] = [0] * (len(parts) - 1)
    elif mode == "minor" and len(parts) >= 2:
        parts[1] += 1
        parts[2:] = [0] * (len(parts) - 2)
    else:  # patch (по умолчанию): последний сегмент
        parts[-1] += 1
    return parts


def main():
    mode = "patch"
    dry_run = False
    for arg in sys.argv[1:]:
        if arg == "--dry-run":
            dry_run = True
        elif arg in ("major", "minor", "patch"):
            mode = arg
        else:
            print(f"Неизвестный аргумент: {arg}", file=sys.stderr)
            return 2

    text = PYPROJECT.read_text(encoding="utf-8")
    m = VERSION_LINE.search(text)
    if not m:
        print("Строка version в pyproject.toml не найдена", file=sys.stderr)
        return 1

    prefix, _quote, *nums, _quote2, suffix = m.groups()
    parts = [int(x) for x in nums if x is not None]
    new = bump(parts, mode)
    new_line = f"{prefix}{_quote}{'.'.join(str(x) for x in new)}{_quote2}{suffix}"

    if not dry_run:
        PYPROJECT.write_text(
            text[: m.start()] + new_line + text[m.end() :], encoding="utf-8"
        )
    print(f"version: {'.'.join(map(str, parts))} -> {'.'.join(map(str, new))} ({mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
