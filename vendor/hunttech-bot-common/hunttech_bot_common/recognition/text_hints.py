"""Text-based hints for document recognition post-processing."""
from __future__ import annotations
import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


def _document_title(lowered: str) -> str:
    """Первая непустая строка документа — титул (заголовок)."""
    for line in lowered.splitlines():
        line = line.strip()
        if line:
            return line
    return lowered.strip()

def _apply_text_hints(data: dict[str, Any], text: str) -> dict[str, Any]:
    if not text:
        return data
    normalized = dict(data)
    lowered = text.lower()
    before_type = normalized.get("document_type")
    _apply_title_hints(normalized, _document_title(lowered))
    if normalized.get("document_type") != before_type:
        logger.info(
            "text_hints: title=%r → document_type=%s (was %s)",
            _document_title(lowered)[:70], normalized.get("document_type"), before_type,
        )
    if "универсальный передаточный документ" in lowered and normalized.get("document_type") not in (
        "CONTRACT", "ADDITIONAL_AGREEMENT", "REPORT", "DECISION",
    ):
        normalized["document_type"] = "UPD"
        normalized["flow_type"] = "PRIMARY"
        logger.info("text_hints: 'универсальный передаточный документ' → UPD")
    if _looks_like_contract(lowered) and normalized.get("document_type") in ("", "UNKNOWN", "OTHER", None):
        normalized["document_type"] = "CONTRACT"
        normalized["flow_type"] = "PRIMARY"
        normalized.pop("receipt_organization", None)
        logger.info("text_hints: contract phrases → CONTRACT")
    if _looks_like_invoice(lowered) and normalized.get("document_type") not in (
        "CONTRACT", "ADDITIONAL_AGREEMENT", "UPD", "ACT", "REPORT", "DECISION",
    ):
        normalized["document_type"] = "INVOICE"
        normalized["flow_type"] = "PRIMARY"
        normalized.pop("receipt_organization", None)
        logger.info("text_hints: invoice phrases → INVOICE")
    if "счет-фактура" in lowered and normalized.get("document_type") not in (
        "CONTRACT", "ADDITIONAL_AGREEMENT", "REPORT", "DECISION",
    ):
        normalized["document_type"] = "UPD"
        normalized["flow_type"] = "PRIMARY"
        logger.info("text_hints: 'счет-фактура' → UPD")
    if (
        "акт выполненных работ" in lowered or "акт сдачи-приемки" in lowered or
        "акт выполненных услуг" in lowered or "акт оказанных услуг" in lowered
    ) and normalized.get("document_type") not in ("CONTRACT", "ADDITIONAL_AGREEMENT", "REPORT", "DECISION", "RECONCILIATION"):
        normalized["document_type"] = "ACT"
        normalized["flow_type"] = "PRIMARY"
        logger.info("text_hints: act phrases → ACT")
    # «Акт сверки» — ОТДЕЛЬНЫЙ тип документа (RECONCILIATION), НИКОГДА не ACT:
    # акт сверки взаимных расчетов — это не акт выполненных работ. Хинт по обороту
    # перебивает ACT/OTHER/UNKNOWN/пустое (если LLM неверно вернул ACT), но не
    # трогает CONTRACT/ADDITIONAL_AGREEMENT/REPORT/DECISION. Substring «сверк»
    # покрывает и «акт_сверки» (имя файла с подчёркиванием), и «сверка/сверки/сверке».
    if (
        "сверк" in lowered or "sverka" in lowered
    ) and normalized.get("document_type") not in ("CONTRACT", "ADDITIONAL_AGREEMENT", "REPORT", "DECISION"):
        normalized["document_type"] = "RECONCILIATION"
        normalized["flow_type"] = "PRIMARY"
        normalized.pop("receipt_organization", None)
        logger.info("text_hints: reconciliation phrases → RECONCILIATION")
    # «Заявление»/«Уведомление» — ОТДЕЛЬНЫЕ типы документов (APPLICATION/NOTICE):
    # уведомление о расторжении договора — это уведомление, а не договор. Хинт по
    # обороту перебивает слабые/неверные ответы LLM (OTHER/UNKNOWN/пустое), но НЕ
    # трогает сильные типы, где слово может встречаться в тексте (заголовок
    # обрабатывается в _apply_title_hints и перебивает безусловно).
    if (
        "уведомлени" in lowered or "заявлени" in lowered
    ) and normalized.get("document_type") not in (
        "CONTRACT", "ADDITIONAL_AGREEMENT", "REPORT", "DECISION", "RECONCILIATION",
        "ACT", "UPD", "INVOICE", "RECEIPT", "TASK", "ORDER", "PROTOCOL", "LETTER",
    ):
        doc_type = "NOTICE" if "уведомлени" in lowered else "APPLICATION"
        normalized["document_type"] = doc_type
        normalized["flow_type"] = "PRIMARY"
        normalized.pop("receipt_organization", None)
        logger.info("text_hints: notice/application phrases → %s", doc_type)
    # «Приказ»/«Протокол»/«Письмо» — ОТДЕЛЬНЫЕ типы документов (ORDER/PROTOCOL/LETTER):
    # приказ о расторжении договора — это приказ, письмо со ссылкой на договор —
    # письмо. Перебивают только слабые ответы LLM, сильные типы не трогают.
    if (
        "приказ" in lowered or "протокол" in lowered or "письм" in lowered
    ) and normalized.get("document_type") not in (
        "CONTRACT", "ADDITIONAL_AGREEMENT", "REPORT", "DECISION", "RECONCILIATION",
        "ACT", "UPD", "INVOICE", "RECEIPT", "TASK", "APPLICATION", "NOTICE",
        "ORDER", "PROTOCOL", "LETTER",
    ):
        if "приказ" in lowered:
            doc_type = "ORDER"
        elif "протокол" in lowered:
            doc_type = "PROTOCOL"
        else:
            doc_type = "LETTER"
        normalized["document_type"] = doc_type
        normalized["flow_type"] = "PRIMARY"
        normalized.pop("receipt_organization", None)
        logger.info("text_hints: order/protocol/letter phrases → %s", doc_type)
    if _looks_like_receipt(text):
        normalized["document_type"] = "RECEIPT"
        normalized["flow_type"] = "ADVANCE_REPORT"
        logger.info("text_hints: receipt phrases → RECEIPT")

    number_date = _extract_document_number_date(text)
    if number_date:
        number, document_date = number_date
        if number and not normalized.get("document_number"):
            normalized["document_number"] = number
        if document_date and not normalized.get("document_date"):
            normalized["document_date"] = document_date

    if normalized.get("flow_type") != "ADVANCE_REPORT":
        buyer = _extract_named_value(text, r"Покупатель:\s*(.+?)(?:\s+\(\d+\)|\n|Адрес:)")
        if buyer and not normalized.get("counterparty_name"):
            normalized["counterparty_name"] = buyer

        primary_party = _extract_primary_party(text)
        if primary_party and not normalized.get("counterparty_name"):
            normalized["counterparty_name"] = primary_party
            normalized["needs_manual_review"] = True
            summary = str(normalized.get("summary") or "").strip()
            note = f"Организация выбрана из OCR как основной кандидат: {primary_party}."
            normalized["summary"] = f"{summary} {note}".strip()

        # Собственная компания HRM не может быть контрагентом: документы, где
        # заказчик/исполнитель — сама компания (акты, УПД, счета), раскладываются
        # в папку ВНЕШНЕЙ стороны. Если LLM вернул «ООО «ХантТек»» — берём
        # внешнюю сторону из текста (Исполнитель/Заказчик/Покупатель/...).
        if _is_own_company(normalized.get("counterparty_name")):
            external = _extract_primary_party(text)
            if external:
                normalized["counterparty_name"] = external
                normalized["needs_manual_review"] = True
                summary = str(normalized.get("summary") or "").strip()
                note = f"Контрагент — внешняя сторона (не HRM): {external}."
                normalized["summary"] = f"{summary} {note}".strip()
                logger.info("text_hints: own-company counterparty → external party %r", external)

    inn = _extract_inn(text)
    if inn and not normalized.get("counterparty_inn"):
        normalized["counterparty_inn"] = inn

    amount = _extract_total_amount(text)
    if amount is not None and normalized.get("amount") in {None, ""}:
        normalized["amount"] = amount
    if "российский рубль" in lowered and not normalized.get("currency"):
        normalized["currency"] = "RUB"
    if normalized.get("flow_type") == "ADVANCE_REPORT":
        current_receipt_organization = normalized.get("receipt_organization")
        hinted_receipt_organization = _extract_receipt_organization(text)
        receipt_organization = current_receipt_organization
        if _is_weak_receipt_organization(current_receipt_organization) and hinted_receipt_organization:
            receipt_organization = hinted_receipt_organization
        if receipt_organization:
            normalized["receipt_organization"] = receipt_organization
            if _is_weak_receipt_counterparty(normalized.get("counterparty_name")):
                normalized["counterparty_name"] = receipt_organization
    return normalized


def _apply_title_hints(normalized: dict[str, Any], title: str) -> None:
    """Титул (первая строка) документа — приоритетный маркер типа.

    Перебивает ответ LLM: заголовок документа важнее, чем упоминание
    «договор»/«счет» в тексте (например, отчёт ссылается на договор-основание).
    """
    title_markers = [
        ("универсальный передаточный документ", "UPD"),
        ("счет-фактура", "UPD"),
        ("счёт-фактура", "UPD"),
        ("дополнительное соглашение", "ADDITIONAL_AGREEMENT"),
        ("допсоглашение", "ADDITIONAL_AGREEMENT"),
        ("договор", "CONTRACT"),
        ("отчет", "REPORT"),
        ("отчёт", "REPORT"),
        ("счет", "INVOICE"),
        ("счёт", "INVOICE"),
        # «Акт сверки» — ОТДЕЛЬНЫЙ тип (RECONCILIATION): маркер ДО «акт» → ACT,
        # иначе «Акт сверки взаимных расчетов» классифицировался бы как ACT.
        ("акт сверки", "RECONCILIATION"),
        ("акт", "ACT"),
        ("решение", "DECISION"),
        ("заявлени", "APPLICATION"),
        ("уведомлени", "NOTICE"),
        ("письм", "LETTER"),
        ("приказ", "ORDER"),
        ("протокол", "PROTOCOL"),
    ]
    for marker, doc_type in title_markers:
        if title.startswith(marker):
            normalized["document_type"] = doc_type
            normalized["flow_type"] = "PRIMARY"
            if doc_type != "ADVANCE_REPORT":
                normalized.pop("receipt_organization", None)
            return


def _looks_like_receipt(text: str) -> bool:
    lowered = text.lower()
    # Более строгая проверка: ФД/ФН/ФП должны быть в контексте кассового чека
    if "кассовый чек" in lowered or "фискальный" in lowered:
        return True
    # ККТ/ОФД — сильные маркеры
    if " ккт " in lowered or " офд " in lowered or "ofd" in lowered:
        return True
    # ФН/ФД/ФП только если рядом слова "накоп"/"чека"/"касс"/"фискал"
    for marker in (" фн ", " фд ", " фп "):
        if marker in lowered:
            ctx_before = lowered[:lowered.index(marker)]
            ctx_after = lowered[lowered.index(marker) + len(marker):]
            nearby = (ctx_before[-100:] + ctx_after[:100]).lower()
            if any(kw in nearby for kw in ("накоп", "чека", "касс", "фискал", "документ", "признак")):
                return True
    return False


def _is_title(text: str, phrase: str) -> bool:
    """Проверяет, что фраза встречается как заголовок (начало текста или строки)."""
    return bool(re.search(rf"(?:^|\n)\s*{re.escape(phrase)}\b", text))


CONTRACT_PHRASES = (
    "договор возмездного оказания услуг",
    "договор оказания услуг",
    "договор подряда",
    "договор аренды",
    "договор поставки",
    "договор купли-продажи",
    "договор на оказание услуг",
    "договор об оказании услуг",
    "договор возмездного",
    "трудовой договор",
    "договор займа",
    "договор цессии",
    "агентский договор",
    "лицензионный договор",
    "договор комиссии",
    "договор поручения",
    "договор дарения",
    "договор хранения",
    "договор фрахтования",
    "договор энергоснабжения",
    "договор страхования",
    "настоящий договор",
    "заключили настоящий договор",
    "по настоящему договору",
    "условия договора",
    "предмет договора",
    "по договору",
    "в соответствии с договором",
    "расторжения договора",
    "исполнения договора",
)


def _looks_like_contract(lowered: str) -> bool:
    """Проверяет, что документ — договор (по заголовку или устойчивым оборотам)."""
    if _is_title(lowered, "договор"):
        return True
    for phrase in CONTRACT_PHRASES:
        if phrase in lowered:
            return True
    # Отдельное слово «договор» в сочетании с признаком заключения/номера
    if re.search(r"\bдоговор\b", lowered) and (
        "заключи" in lowered or "договор №" in lowered or "договор n" in lowered
    ):
        return True
    return False


def _looks_like_invoice(lowered: str) -> bool:
    """Проверяет, похож ли текст на счёт на оплату (PRIMARY, не ADVANCE_REPORT).

    Не срабатывает на договорах: в них тоже есть «расчетный счет» и «Заказчик».
    """
    if "счет на оплату" in lowered or "счёт на оплату" in lowered:
        return True
    if _is_title(lowered, "счет") or _is_title(lowered, "счёт"):
        return True
    # Слабая ветка — только если в тексте НЕТ признаков договора
    if "договор" in lowered or "дог." in lowered:
        return False
    if ("счет" in lowered or "счёт" in lowered) and ("расчетный счет" in lowered or "р/с" in lowered or "банк" in lowered):
        if "покупатель" in lowered or "заказчик" in lowered or "плательщик" in lowered:
            return True
    return False


def _extract_document_number_date(text: str) -> tuple[str | None, str | None] | None:
    patterns = [
        r"Документ об отгрузке\s+Универсальный передаточный документ,\s*№\s*([^\s]+)\s*от\s*(\d{2}\.\d{2}\.\d{4})",
        r"Счет-фактура\s*№\s*([^\s]+)\s*от\s*(\d{1,2})\s+([а-яА-Я]+)\s+(\d{4})",
    ]
    match = re.search(patterns[0], text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(), _date_from_ddmmyyyy(match.group(2))
    match = re.search(patterns[1], text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(), _date_from_russian_month(match.group(2), match.group(3), match.group(4))
    return None

def _extract_named_value(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    value = " ".join(match.group(1).split())
    return value.strip(" ,;") or None

def _is_own_company(value: Any) -> bool:
    """Является ли сторона собственной компанией HRM (ООО «ХантТек»/ХантТек)."""
    text = str(value or "").strip().lower()
    if not text:
        return False
    text = text.replace("«", "").replace("»", "").replace('"', "").replace("'", "")
    return "ханттек" in text or "hunttech" in text

def _extract_primary_party(text: str) -> str | None:
    preferred_labels = ("Покупатель", "Заказчик", "Клиент", "Плательщик")
    secondary_labels = ("Исполнитель", "Поставщик", "Продавец")
    for label in preferred_labels + secondary_labels:
        party = _extract_party_by_label(text, label)
        # Собственная компания HRM никогда не является контрагентом —
        # документы раскладываются в папку ВНЕШНЕЙ стороны.
        if party and not _is_own_company(party):
            return party
    entity = _extract_first_legal_entity(text)
    if entity and not _is_own_company(entity):
        return entity
    return None

def _extract_party_by_label(text: str, label: str) -> str | None:
    stop_labels = (
        "Исполнитель",
        "Поставщик",
        "Продавец",
        "Заказчик",
        "Покупатель",
        "Клиент",
        "Плательщик",
        "Адрес",
        "ИНН",
        "КПП",
    )
    stop = "|".join(stop_labels)
    match = re.search(
        rf"{label}\s*[:\-]?\s*(.+?)(?=\s+(?:{stop})\b|[\n;]|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _clean_organization_name(match.group(1))



def _extract_first_legal_entity(text: str) -> str | None:
    match = re.search(
        r"\b(?:ООО|АО|ПАО|ЗАО|ОАО)\s*(?:[«\"“][^»\"”]{2,120}[»\"”]|[А-ЯA-ZЁ][^,\n;]{2,120})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _clean_organization_name(match.group(0))


def _extract_receipt_organization(text: str) -> str | None:
    labels = (
        "Пользователь",
        "Продавец",
        "Поставщик",
        "Организация",
        "Торговая точка",
        "Место расчетов",
        "Место расчета",
    )
    for label in labels:
        value = _extract_named_value(text, rf"{label}\s*[:\-]?\s*(.+?)(?:\n|ИНН|КПП|Кассир|ККТ|ФН|ФД|ФП|$)")
        cleaned = _clean_organization_name(value or "")
        if cleaned and not _is_weak_receipt_organization(cleaned):
            return cleaned

    legal_entity = _extract_first_legal_entity(text)
    if legal_entity:
        return legal_entity

    brand = _extract_known_receipt_brand(text)
    if brand:
        return brand
    return None


def _extract_known_receipt_brand(text: str) -> str | None:
    known_brands = (
        "ЛУКОЙЛ",
        "ГАЗПРОМНЕФТЬ",
        "РОСНЕФТЬ",
        "ТАТНЕФТЬ",
        "SHELL",
        "ЯНДЕКС GO",
        "ЯНДЕКС.ТАКСИ",
    )
    upper_text = text.upper()
    for brand in known_brands:
        if brand in upper_text:
            return brand
    return None



def _is_weak_receipt_organization(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if len(text) <= 2:
        return True
    if _extract_first_legal_entity(text):
        return False
    if _extract_known_receipt_brand(text):
        return False
    return _looks_like_person_name(text)



def _is_weak_receipt_counterparty(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if len(text) <= 2:
        return True
    if _looks_like_person_name(text):
        return True
    return False


def _looks_like_person_name(value: str) -> bool:
    return bool(re.fullmatch(r"[А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z]\.?\s*[А-ЯЁA-Z]?\.?)?", value.strip()))


def _clean_organization_name(value: str) -> str | None:
    cleaned = " ".join(value.split())
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" ,;:.")
    org_match = re.search(
        r"\b(?:ООО|АО|ПАО|ЗАО|ОАО)\s*(?:[«\"“][^»\"”]{2,120}[»\"”]|[А-ЯA-ZЁ][^,\n;]{2,120})",
        cleaned,
        flags=re.IGNORECASE,
    )
    if org_match:
        cleaned = org_match.group(0).strip(" ,;:.")
    return cleaned or None



def _extract_inn(text: str) -> str | None:
    patterns = [
        r"ИНН/КПП\s+покупателя:\s*(\d{10,12})",
        r"ИНН/КПП\s+заказчика:\s*(\d{10,12})",
        r"ИНН\s*[:№]?\s*(\d{10,12})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None



def _extract_total_amount(text: str) -> float | None:
    match = re.search(r"Всего к оплате.*?([0-9][0-9\s]*,\d{2})\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return _parse_russian_amount(match.group(1))


def _parse_russian_amount(value: str) -> float | None:
    """Парсит сумму из строки вида \"15 000,50\" в float.
    Если AI вернул число (float/int) — преобразует через str().
    """
    try:
        str_value = str(value)
        return float(str_value.replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None



def _date_from_ddmmyyyy(value: str) -> str | None:
    try:
        day, month, year = [int(part) for part in value.split(".")]
        return date(year, month, day).isoformat()
    except ValueError:
        return None



def _date_from_russian_month(day: str, month_name: str, year: str) -> str | None:
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    month = months.get(month_name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def _normalize_result(data: dict[str, Any], *, hint_text: str = "") -> dict[str, Any]:
    normalized = dict(data)
    normalized["flow_type"] = _normalize_enum(
        normalized.get("flow_type"),
        {
            "primary": "PRIMARY",
            "advance_report": "ADVANCE_REPORT",
            "advance report": "ADVANCE_REPORT",
            "receipt": "ADVANCE_REPORT",
        },
        _infer_flow_type(hint_text),
    )
    normalized["document_type"] = _normalize_enum(
        normalized.get("document_type"),
        {
            "contract": "CONTRACT",
            "договор": "CONTRACT",
            "report": "REPORT",
            "отчет": "REPORT",
            "отчёт": "REPORT",
            "act": "ACT",
            "акт": "ACT",
            "application": "APPLICATION",
            "заявление": "APPLICATION",
            "заявлени": "APPLICATION",
            "notice": "NOTICE",
            "уведомление": "NOTICE",
            "уведомлени": "NOTICE",
            "order": "ORDER",
            "приказ": "ORDER",
            "prikaz": "ORDER",
            "protocol": "PROTOCOL",
            "протокол": "PROTOCOL",
            "protokol": "PROTOCOL",
            "letter": "LETTER",
            "письмо": "LETTER",
            "письм": "LETTER",
            "upd": "UPD",
            "упд": "UPD",
            "invoice": "INVOICE",
            "счет": "INVOICE",
            "счёт": "INVOICE",
            "task": "TASK",
            "задание": "TASK",
            "receipt": "RECEIPT",
            "чек": "RECEIPT",
            "other": "OTHER",
        },
        _infer_document_type(hint_text),
    )
    normalized.setdefault("needs_manual_review", True)
    normalized["confidence"] = _normalize_confidence(normalized.get("confidence"))
    return normalized


def _normalize_enum(value: Any, mapping: dict[str, str], default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    upper = text.upper()
    allowed = set(mapping.values()) | {default}
    if upper in allowed:
        return upper
    return mapping.get(text.lower(), default)


def _normalize_confidence(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    text = str(value or "").strip().lower()
    if text in {"high", "высокая", "высокий"}:
        return 0.85
    if text in {"medium", "средняя", "средний"}:
        return 0.55
    if text in {"low", "низкая", "низкий"}:
        return 0.25
    try:
        return max(0.0, min(1.0, float(text)))
    except ValueError:
        return 0.0


def _infer_flow_type(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("чек", "авансов", "топливо", "азс", "такси")):
        return "ADVANCE_REPORT"
    if lowered.strip():
        return "PRIMARY"
    return "UNKNOWN"


def _infer_document_type(text: str) -> str:
    lowered = text.lower()
    # Сначала — маркеры в имени файла / первых строках (hint_text начинается
    # с original_name). «Отчет»/«отчёт» идут ПЕРЕД «договор»: отчёт часто
    # ссылается на договор-основание («в рамках Договора №...»), и substring
    # «договор» ловил бы падежную форму «Договора» в тексте отчёта.
    head = lowered[:150]
    # «Сверк»/«sverka» в имени файла — RECONCILIATION (без \b-границы: «акт_сверки»
    # с подчёркиванием и латиница «sverka» иначе не матчатся).
    if "сверк" in head or "sverka" in head:
        return "RECONCILIATION"
    head_markers = [
        ("универсальный передаточный документ", "UPD"),
        ("счет-фактура", "UPD"),
        ("счёт-фактура", "UPD"),
        ("дополнительное соглашение", "ADDITIONAL_AGREEMENT"),
        ("допсоглашение", "ADDITIONAL_AGREEMENT"),
        ("отчет", "REPORT"),
        ("отчёт", "REPORT"),
        ("otchet", "REPORT"),
        ("report", "REPORT"),
        ("решени", "DECISION"),
        ("reshenie", "DECISION"),
        ("decision", "DECISION"),
        # «Заявление»/«Уведомление»/«Письмо»/«Приказ»/«Протокол» — ОТДЕЛЬНЫЕ типы
        # (APPLICATION/NOTICE/LETTER/ORDER/PROTOCOL): маркер ДО «договор», иначе
        # «Уведомление о расторжении договора» дало бы CONTRACT.
        ("заявлени", "APPLICATION"),
        ("уведомлени", "NOTICE"),
        ("письм", "LETTER"),
        ("letter", "LETTER"),
        ("приказ", "ORDER"),
        ("prikaz", "ORDER"),
        ("order", "ORDER"),
        ("протокол", "PROTOCOL"),
        ("protokol", "PROTOCOL"),
        ("protocol", "PROTOCOL"),
        ("договор", "CONTRACT"),
        ("dogovor", "CONTRACT"),
        ("contract", "CONTRACT"),
        # «Акт сверки»/«сверка» — ОТДЕЛЬНЫЙ тип (RECONCILIATION): маркер ДО «акт»,
        # иначе «акт сверки» в имени файла дал бы ACT.
        ("сверк", "RECONCILIATION"),
        ("акт", "ACT"),
        ("счет", "INVOICE"),
        ("счёт", "INVOICE"),
        ("schet", "INVOICE"),
        ("invoice", "INVOICE"),
        ("задани", "TASK"),
        ("чек", "RECEIPT"),
        ("receipt", "RECEIPT"),
    ]
    for marker, document_type in head_markers:
        if re.search(rf"\b{re.escape(marker)}", head):
            return document_type
    markers = [
        ("упд", "UPD"),
        ("отчет", "REPORT"),
        ("отчёт", "REPORT"),
        ("otchet", "REPORT"),
        ("report", "REPORT"),
        ("решени", "DECISION"),
        ("reshenie", "DECISION"),
        ("decision", "DECISION"),
        ("заявлени", "APPLICATION"),
        ("уведомлени", "NOTICE"),
        ("письм", "LETTER"),
        ("letter", "LETTER"),
        ("приказ", "ORDER"),
        ("prikaz", "ORDER"),
        ("order", "ORDER"),
        ("протокол", "PROTOCOL"),
        ("protokol", "PROTOCOL"),
        ("protocol", "PROTOCOL"),
        ("договор", "CONTRACT"),
        ("dogovor", "CONTRACT"),
        ("contract", "CONTRACT"),
        ("допсоглашени", "ADDITIONAL_AGREEMENT"),
        ("сверк", "RECONCILIATION"),
        ("sverka", "RECONCILIATION"),
        ("акт", "ACT"),
        ("счет", "INVOICE"),
        ("счёт", "INVOICE"),
        ("schet", "INVOICE"),
        ("invoice", "INVOICE"),
        ("задани", "TASK"),
        ("чек", "RECEIPT"),
        ("receipt", "RECEIPT"),
    ]
    for marker, document_type in markers:
        if marker in lowered:
            return document_type
    return "UNKNOWN"
