"""Расчёт почасовых ставок кандидатов по ставке заказчика (стандарт HuntTech).

Алгоритм перенесён из hunttech_short_vavancy_bot (команда ``/rates``,
одобрен владельцем) и является ЕДИНСТВЕННЫМ источником расчёта ставок
для всех HuntTech-ботов. Боты вызывают ``calculate_candidate_rate``
из общей библиотеки, а не держат свои копии SQL/арифметики.

Логика::

    1. Вход — ставка заказчика (руб/час, число).
    2. Поиск строки в справочнике HUNTTECH_OUTSTAFFING_RATES:
       - точное совпадение по столбцу ``rate`` (только активные строки,
         ``delete_ts IS NULL``);
       - если нет — ближайшая МЕНЬШАЯ ставка (``ORDER BY rate DESC LIMIT 1``).
    3. Из найденной строки: ``max_salary`` (зарплата по ТК, руб/мес)
       и ``max_ie_salary`` (выплата ИП, руб/мес).
    4. Оформление вакансии определяет, какие ставки показывать:
       - «ГПХ» → только по ТК (``max_salary``);
       - «ИП» → только по ИП (``max_ie_salary``);
       - «ГПХ или ИП» (или не указано) → обе.
    5. Часовая ставка = зарплата ÷ 164 (часов в рабочем месяце),
       округление ВНИЗ до ближайших 100 руб.
    6. Отчёт в формате «Вознаграждение» (шаблон канала
       t.me/hunttech_shortproject/46) — только почасовые ставки,
       «на руки» для ГПХ, пометка про налоги для ИП.

В базу данных НИЧЕГО не пишется — только чтение справочника.

Требуется объект БД с ``async with db.acquire() as conn`` (например
``hunttech_bot_common.database.DatabasePool``); ``conn`` должен быть
asyncpg-совместимым (``fetchrow``).

Usage::

    from hunttech_bot_common.services.rates import calculate_candidate_rate

    result = await calculate_candidate_rate(app.db, 2500, empl="ГПХ")
    if "error" in result:
        # "no_db" | "not_found" | "db_error"
        ...
    report = result["report"]   # готовый текст для отправки пользователю
    rate_val = result["rate_val"]  # «500» или «500 / 600» для подстановки
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Константы алгоритма ───────────────────────────────────────────

OUTSTAFFING_RATES_TABLE = "HUNTTECH_OUTSTAFFING_RATES"
"""Справочник «Рейты по аутстафу» (ставка заказчика → зарплата)."""

HOURLY_MONTH_HOURS = 164
"""Часов в рабочем месяце (делитель для почасовой ставки)."""

HOURLY_ROUND_STEP = 100
"""Шаг округления почасовой ставки вниз (руб.)."""

EMPLOYMENT_GPH = "ГПХ"
EMPLOYMENT_IP = "ИП"
EMPLOYMENT_BOTH = "ГПХ или ИП"

# ── Чистые функции ────────────────────────────────────────────────


def hourly_from_monthly(monthly: int) -> int:
    """Часовая ставка из месячной: ÷164, округление вниз до 100 руб.

    Примеры: 87900 → 500, 91200 → 500 (не 600), 164000 → 1000.
    """
    return (monthly // HOURLY_MONTH_HOURS // HOURLY_ROUND_STEP) * HOURLY_ROUND_STEP


def pick_employment_rates(empl: str, hourly_tk: int, hourly_ip: int) -> dict[str, Any]:
    """Какие ставки показывать по оформлению вакансии и значение для подстановки.

    «ГПХ» → по ТК, «ИП» → по ИП, «ГПХ или ИП» (или не указано/другое) → обе
    (для подстановки — через « / »).

    Returns:
        {"want_tk": bool, "want_ip": bool, "rate_val": str}
    """
    empl_norm = (empl or "").strip()
    # Неизвестное оформление (или не указано) → обе ставки: так rate_val
    # и want_* остаются согласованными (пустой отчёт невозможен).
    unknown = empl_norm not in (EMPLOYMENT_GPH, EMPLOYMENT_IP, EMPLOYMENT_BOTH)
    want_tk = unknown or empl_norm in (EMPLOYMENT_GPH, EMPLOYMENT_BOTH)
    want_ip = unknown or empl_norm in (EMPLOYMENT_IP, EMPLOYMENT_BOTH)
    if empl_norm == EMPLOYMENT_IP:
        rate_val = str(hourly_ip)
    elif empl_norm == EMPLOYMENT_GPH:
        rate_val = str(hourly_tk)
    else:
        rate_val = f"{hourly_tk} / {hourly_ip}"
    return {"want_tk": want_tk, "want_ip": want_ip, "rate_val": rate_val}


def build_candidate_rates_report(
    user_rate: int,
    match_type: str,
    db_rate: int,
    hourly_tk: int,
    hourly_ip: int,
    empl: str,
    want_tk: bool,
    want_ip: bool,
) -> str:
    """Отчёт «Расчёт ставки кандидата» в формате «Вознаграждение».

    Формат зафиксирован шаблоном канала t.me/hunttech_shortproject/46.
    """
    match_label = (
        "🔍 Найдено точное совпадение"
        if match_type == "exact"
        else f"🔍 Точного совпадения нет — взята ближайшая меньшая ставка (*{db_rate}* руб/час)"
    )
    lines = [
        "✅ *Расчёт ставки кандидата*\n",
        f"💰 Ставка заказчика: *{int(user_rate)}* руб/час",
        f"{match_label}\n",
        "💰 *Вознаграждение:*",
    ]
    if want_tk:
        lines.append(f"🤝 ГПХ — *{hourly_tk}* руб./час на руки.")
    if want_ip:
        lines.append(
            f"💼 ИП — *{hourly_ip}* руб./час, налоги оплачиваются специалистом самостоятельно."
        )
    if (empl or "").strip() in (EMPLOYMENT_GPH, EMPLOYMENT_IP):
        lines.append(f"\n📌 Оформление в вакансии: *{empl.strip()}* — показана только эта ставка.")
    return "\n".join(lines)


# ── Доступ к справочнику ──────────────────────────────────────────


async def lookup_outstaffing_rate(conn: Any, user_rate: int | float) -> dict | None:
    """Найти строку справочника по ставке заказчика.

    Точное совпадение ``rate``, иначе ближайшая меньшая. Только активные
    строки (``delete_ts IS NULL``).

    Returns:
        {"rate": int, "max_salary": int, "max_ie_salary": int, "match_type": str}
        или None, если строк нет.
    """
    sql_exact = (
        f"SELECT rate, max_salary, max_ie_salary FROM {OUTSTAFFING_RATES_TABLE} "
        "WHERE rate = $1 AND delete_ts IS NULL"
    )
    sql_lower = (
        f"SELECT rate, max_salary, max_ie_salary FROM {OUTSTAFFING_RATES_TABLE} "
        "WHERE rate < $1 AND delete_ts IS NULL ORDER BY rate DESC LIMIT 1"
    )
    row = await conn.fetchrow(sql_exact, user_rate)
    match_type = "exact"
    if not row:
        match_type = "nearest_lower"
        row = await conn.fetchrow(sql_lower, user_rate)
    if not row:
        return None
    return {
        "rate": int(row["rate"]),
        "max_salary": int(row["max_salary"]),
        "max_ie_salary": int(row["max_ie_salary"]),
        "match_type": match_type,
    }


# ── Высокоуровневый расчёт (единая точка вызова для всех ботов) ───


async def calculate_candidate_rate(db: Any, user_rate: int | float, empl: str = "") -> dict[str, Any]:
    """Полный расчёт ставки кандидата по ставке заказчика.

    Args:
        db: объект БД с ``async with db.acquire() as conn``
            (``DatabasePool`` или аналогичный asyncpg-пул).
        user_rate: ставка заказчика (руб/час).
        empl: оформление из вакансии — «ГПХ», «ИП», «ГПХ или ИП» или "".

    Returns:
        При успехе — dict с ключами: user_rate, match_type, db_rate,
        salary_tk, salary_ip, hourly_tk, hourly_ip, empl, want_tk,
        want_ip, rate_val, report.
        При ошибке — {"error": "no_db" | "not_found" | "db_error",
        "detail": str}.
    """
    if db is None or not hasattr(db, "acquire"):
        logger.warning("Rates: db not available (no acquire)")
        return {"error": "no_db"}

    try:
        async with db.acquire() as conn:
            found = await lookup_outstaffing_rate(conn, user_rate)

        if not found:
            return {"error": "not_found"}

        hourly_tk = hourly_from_monthly(found["max_salary"])
        hourly_ip = hourly_from_monthly(found["max_ie_salary"])

        pick = pick_employment_rates(empl, hourly_tk, hourly_ip)
        report = build_candidate_rates_report(
            user_rate=int(user_rate),
            match_type=found["match_type"],
            db_rate=found["rate"],
            hourly_tk=hourly_tk,
            hourly_ip=hourly_ip,
            empl=empl,
            want_tk=pick["want_tk"],
            want_ip=pick["want_ip"],
        )

        return {
            "user_rate": int(user_rate),
            "match_type": found["match_type"],
            "db_rate": found["rate"],
            "salary_tk": found["max_salary"],
            "salary_ip": found["max_ie_salary"],
            "hourly_tk": hourly_tk,
            "hourly_ip": hourly_ip,
            "empl": (empl or "").strip(),
            "want_tk": pick["want_tk"],
            "want_ip": pick["want_ip"],
            "rate_val": pick["rate_val"],
            "report": report,
        }
    except Exception as e:  # noqa: BLE001 — отдаём ошибку боту, а не роняем его
        logger.exception("Rates DB query failed: %s", e)
        return {"error": "db_error", "detail": str(e)}
