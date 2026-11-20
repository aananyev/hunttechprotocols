"""AI usage tracking — учёт обращений всех HuntTech-ботов к нейросети.

Единый реестр всех AI-вызовов всех ботов: файл
`~/.hermes/hunttech_bots/ai_usage.json` (общий — администратор смотрит
сводный отчёт по всем ботам). Каждый `AIClient.complete()` записывает
UsageRecord: бот, пользователь (владелец ключа), провайдер, модель, задача
(task), статус, токены, длительность, стоимость (USD по прайсу моделей).

Отчёт администратору — команда `/usage` в каждом боте (см.
`format_usage_report`): в разрезе модели / пользователя / задачи /
провайдера / по дням, за период (день / неделя / месяц / всё время).

Правило (стандарт HuntTech, 08.2026):
- хранилище ОДНО на все боты (общий файл) — сводный отчёт;
- запись — из библиотеки (AIClient), боты ничего не логируют сами;
- стоимость — по прайсу MODEL_PRICES (USD за 1 млн токенов); неизвестная
  модель → 0 USD, но токены и запросы учитываются всегда;
- текст отчёта — plain text (parse_mode=None), как все сообщения ботов.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Общий файл учёта всех HuntTech-ботов (рядом с access_users.json).
DEFAULT_USAGE_PATH = (
    Path.home() / ".hermes" / "hunttech_bots" / "ai_usage.json"
)

# Прайс моделей: (USD за 1 млн входных токенов, USD за 1 млн выходных).
# Ключ — подстрока имени модели (lower); при поиске берётся САМЫЙ ДЛИННЫЙ
# совпавший ключ («gpt-4o-mini» не должен попасть под «gpt-4o»).
# Цены — справочные на 08.2026; при изменении прайса провайдера — править
# здесь (единая точка).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # DeepSeek
    "deepseek-reasoner": (0.55, 2.19),
    "deepseek-v4-flash": (0.27, 1.10),
    "deepseek-chat": (0.27, 1.10),
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    # Anthropic
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
    # Google
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # Прочие / неизвестные — токены учитываются, стоимость 0
}

_MAX_RECORDS = 20000  # потолок записей в файле (старые обрезаются)


def estimate_cost(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Оценка стоимости запроса в USD по прайсу MODEL_PRICES.

    Берётся самый длинный ключ-подстрока, совпавший с именем модели
    (специфичные имена имеют приоритет над общими). Неизвестная модель —
    (0.0, 0.0): стоимость 0, токены учитываются.
    """
    m = (model or "").lower()
    price: tuple[float, float] = (0.0, 0.0)
    best_len = -1
    for key, p in MODEL_PRICES.items():
        if key in m and len(key) > best_len:
            price = p
            best_len = len(key)
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


@dataclass
class UsageRecord:
    """Одна запись обращения к нейросети.

    user_id/username — владелец активного ключа (чей конфиг использован:
    личные креды пользователя или админ .env). task — бизнес-задача
    (имя AI-функции, передаётся в AIClient.complete(task=...)).
    """

    bot_name: str = ""
    user_id: int | None = None
    username: str = ""
    provider: str = ""
    model: str = ""
    task: str = "unknown"
    status: str = "ok"  # ok | error
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0
    cost_usd: float = 0.0
    source: str = ""  # личные | админ (.env)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="seconds"))

    @classmethod
    def from_dict(cls, d: dict) -> "UsageRecord":
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def to_dict(self) -> dict:
        return asdict(self)


class UsageTracker:
    """Реестр обращений к нейросети (JSON-файл, общий для всех ботов).

    Безопасность записи: межпроцессная блокировка fcntl.flock (боты —
    отдельные процессы) + атомарная замена файла (tmp + os.replace).
    fcntl недоступен (Windows) — блокировка пропускается (локальный кейс
    macOS/Linux, где fcntl есть).
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_USAGE_PATH,
        max_records: int = _MAX_RECORDS,
    ) -> None:
        self.path = Path(path)
        self.max_records = max_records
        self._lock = threading.Lock()

    # ── запись ──────────────────────────────────────────────────────

    def append(self, record: UsageRecord | dict) -> None:
        """Добавить запись (с межпроцессной блокировкой и триммингом)."""
        rec = record.to_dict() if isinstance(record, UsageRecord) else dict(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            try:
                with open(self.path, "a+", encoding="utf-8") as fh:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX)
                    except (OSError, AttributeError):  # нет fcntl — без блокировки
                        pass
                    data = self._read_locked(fh)
                    data.append(rec)
                    if len(data) > self.max_records:
                        data = data[-self.max_records:]
                    self._write_locked(fh, data)
                    try:
                        fcntl.flock(fh, fcntl.LOCK_UN)
                    except (OSError, AttributeError):
                        pass
            except OSError as e:
                logger.warning("ai_usage append failed: %s", e)

    def _read_locked(self, fh) -> list[dict]:
        fh.seek(0)
        raw = fh.read()
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError as e:
            logger.warning("ai_usage corrupt (%s) — начинаю новый реестр", e)
            return []

    def _write_locked(self, fh, data: list[dict]) -> None:
        # Атомарно: пишем во временный файл и заменяем — читатели (отчёт)
        # никогда не видят полузаписанный JSON.
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ── чтение / отчёт ──────────────────────────────────────────────

    def records(self, period: str = "all") -> list[dict]:
        """Записи за период: day | week | month | all | число-дней."""
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("ai_usage read failed: %s", e)
            return []
        cutoff = _period_cutoff(period)
        if cutoff is None:
            return data
        return [r for r in data if _created_at(r) >= cutoff]

    def summarize(self, period: str = "all") -> dict[str, Any]:
        """Агрегаты за период: итоги + разрезы по модели/пользователю/
        задаче/провайдеру/дню."""
        rows = self.records(period)
        totals = {
            "requests": len(rows),
            "ok_requests": 0,
            "error_requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
        by_model: dict[str, dict] = {}
        by_user: dict[str, dict] = {}
        by_task: dict[str, dict] = {}
        by_provider: dict[str, dict] = {}
        by_day: dict[str, dict] = {}

        def _buckets(acc: dict, key: str) -> dict:
            b = acc.setdefault(key, {
                "requests": 0, "total_tokens": 0, "cost_usd": 0.0})
            return b

        for r in rows:
            status = r.get("status") or "ok"
            if status == "ok":
                totals["ok_requests"] += 1
            else:
                totals["error_requests"] += 1
            pt = int(r.get("prompt_tokens") or 0)
            ct = int(r.get("completion_tokens") or 0)
            tt = int(r.get("total_tokens") or 0)
            cost = float(r.get("cost_usd") or 0.0)
            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["total_tokens"] += tt
            totals["cost_usd"] += cost

            for acc, key in (
                (by_model, (r.get("model") or "unknown")),
                (by_user, _user_label(r)),
                (by_task, (r.get("task") or "unknown")),
                (by_provider, (r.get("provider") or "unknown")),
                (by_day, (r.get("created_at") or "")[:10] or "unknown"),
            ):
                b = _buckets(acc, key)
                b["requests"] += 1
                b["total_tokens"] += tt
                b["cost_usd"] += cost

        def _sort(acc: dict) -> list[tuple[str, dict]]:
            return sorted(acc.items(), key=lambda kv: -kv[1]["cost_usd"])

        return {
            "totals": totals,
            "by_model": _sort(by_model),
            "by_user": _sort(by_user),
            "by_task": _sort(by_task),
            "by_provider": _sort(by_provider),
            "by_day": sorted(by_day.items(), key=lambda kv: kv[0]),
        }

    def clear(self) -> None:
        """Очистить реестр (тесты)."""
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def _period_cutoff(period: str) -> datetime | None:
    """Начало периода (UTC) или None для 'all'."""
    p = (period or "all").strip().lower()
    if p == "all":
        return None
    if p == "day":
        days = 1
    elif p == "week":
        days = 7
    elif p == "month":
        days = 30
    else:
        try:
            days = max(1, int(p))
        except (TypeError, ValueError):
            days = 1
    return datetime.now(timezone.utc) - timedelta(days=days)


def _created_at(r: dict) -> datetime:
    raw = r.get("created_at") or ""
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _user_label(r: dict) -> str:
    uid = r.get("user_id")
    uname = (r.get("username") or "").strip()
    if uname:
        return f"@{uname} (id {uid})" if uid is not None else f"@{uname}"
    return f"id {uid}" if uid is not None else "unknown"


def _period_label(period: str) -> str:
    p = (period or "all").strip().lower()
    if p == "day":
        return "сегодня"
    if p == "week":
        return "за 7 дней"
    if p == "month":
        return "за 30 дней"
    if p == "all":
        return "за всё время"
    try:
        return f"за {int(p)} дней"
    except (TypeError, ValueError):
        return f"за период «{period}»"


def _fmt_cost(cost: float) -> str:
    if cost >= 100:
        return f"${cost:,.0f}"
    if cost >= 1:
        return f"${cost:,.2f}"
    return f"${cost:,.4f}"


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} млн"
    if n >= 1_000:
        return f"{n / 1_000:.0f} тыс."
    return str(n)


def usage_period_from_args(args: list[str] | None) -> str:
    """Период из аргументов команды /usage.

    /usage → day; /usage week|month|all → соответствующий; /usage N →
    N дней; неизвестный аргумент → day.
    """
    for a in (args or []):
        a = a.strip().lower()
        if a in ("day", "week", "month", "all"):
            return a
        if a.isdigit():
            return a
    return "day"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: 1 запрос, 2 запроса, 5 запросов."""
    n10, n100 = n % 10, n % 100
    if 10 <= n100 <= 20:
        return many
    if n10 == 1:
        return one
    if 2 <= n10 <= 4:
        return few
    return many


def format_usage_report(
    tracker: UsageTracker,
    period: str = "day",
    bot_name: str | None = None,
) -> str:
    """Итоговый отчёт по использованию нейросети (plain text).

    Разрезы: модель, пользователь, задача, провайдер, по дням. Период —
    day | week | month | all | число-дней (usage_period_from_args).
    """
    s = tracker.summarize(period)
    t = s["totals"]
    title = bot_name or "HuntTech-боты"

    req_word = _plural(t["requests"], "запрос", "запроса", "запросов")
    err_word = _plural(t["error_requests"], "ошибка", "ошибки", "ошибок")

    lines = [
        f"💰 Расходы на нейросеть — {title}",
        f"Период: {_period_label(period)}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        (f"ИТОГО: {_fmt_int(t['requests'])} {req_word}"
         f" ({_fmt_int(t['ok_requests'])} ok /"
         f" {_fmt_int(t['error_requests'])} {err_word})"),
        (f"Токены: {_fmt_tokens(t['total_tokens'])}"
         f" (вход {_fmt_tokens(t['prompt_tokens'])} /"
         f" выход {_fmt_tokens(t['completion_tokens'])})"),
        f"Стоимость: {_fmt_cost(t['cost_usd'])}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    def _section(title: str, rows: list[tuple[str, dict]], show_tokens: bool = True):
        if not rows:
            return
        lines.append(f"{title}")
        for name, b in rows[:15]:
            cost = _fmt_cost(b["cost_usd"])
            tok = f" · {_fmt_tokens(b['total_tokens'])} ток." if show_tokens else ""
            lines.append(
                f"  {name} — {_fmt_int(b['requests'])} запр.{tok} · {cost}")
        if len(rows) > 15:
            lines.append(f"  … и ещё {len(rows) - 15}")

    _section("🧠 По моделям:", s["by_model"])
    _section("👤 По пользователям:", s["by_user"])
    _section("📋 По задачам:", s["by_task"])
    _section("🏢 По провайдерам:", s["by_provider"], show_tokens=False)
    _section("📅 По дням:", s["by_day"], show_tokens=False)

    return "\n".join(lines)
