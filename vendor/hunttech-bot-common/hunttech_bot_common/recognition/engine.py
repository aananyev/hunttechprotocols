"""Recognition engine — orchestrates document processing pipeline."""
from __future__ import annotations
import asyncio
import json
import logging
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# PDF с извлечённым текстом короче этого порога считается «тонким текстовым
# слоем» (например, чек check.yandex.ru: в тексте только «Чек» + URL + футер
# браузера «1 of 1», а реквизиты — картинкой внутри PDF). Для таких PDF
# извлечённый текст бесполезен — добавляем OCR рендера первой страницы
# (кейс 17.08.2026, запись 98edfabb0660-2265: контрагент и сумма не
# распознались из мусорного слоя).
_PDF_TEXT_MIN_CHARS = 200

try:
    from agent.plugin_llm import PluginLlmTextInput
except Exception:
    PluginLlmTextInput = None

from hunttech_bot_common.recognition.schemas import (
    DOCUMENT_SCHEMA, INSTRUCTIONS, RecognitionResult,
    _extract_json_object, _parse_json_fallback,
    _guess_mime, _is_pdf, _is_supported_image,
    _extract_text, _normalize_result,
)
from hunttech_bot_common.recognition.text_hints import _apply_text_hints
from hunttech_bot_common.recognition.ocr_space import recognize_text_with_ocr_space
from hunttech_bot_common.recognition.yandex_vision import recognize_text_with_yandex_vision
from hunttech_bot_common.ai.usage import UsageRecord, estimate_cost


def _track_recognition_usage(
    usage_tracker: Any,
    *,
    bot_name: str,
    user_id: int | None,
    username: str | None,
    status: str,
    provider: str,
    model: str,
    usage: Any,
) -> None:
    """Запись обращения к LLM в общий реестр расходов (0.6.2).

    Хост-результат PluginLlm (complete_structured) уже несёт provider,
    model и usage (input/output/total_tokens + cost_usd — оценка хоста).
    Учёт никогда не роняет распознавание (try/except + warning).
    """
    if usage_tracker is None:
        return
    try:
        u = usage or {}
        input_tokens = int(getattr(u, "input_tokens", 0) or 0)
        output_tokens = int(getattr(u, "output_tokens", 0) or 0)
        total = int(getattr(u, "total_tokens", 0) or 0)
        cost = getattr(u, "cost_usd", None)
        if cost is None:
            cost = estimate_cost(model, input_tokens, output_tokens)
        record = UsageRecord(
            bot_name=bot_name,
            user_id=user_id,
            username=username or "",
            provider=provider or "unknown",
            model=model or "unknown",
            task="recognize_document",
            status=status,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=total,
            duration_ms=0.0,
            cost_usd=float(cost or 0.0),
            source="plugin_llm",
        )
        usage_tracker.append(record)
    except Exception as exc:
        logger.warning("ai usage track failed: %s", exc)

def recognize_document(
    llm: Any,
    path: Path,
    *,
    caption: str = "",
    original_name: str = "",
    settings: Any = None,
    progress_callback: Callable[[str, str], None] | None = None,
    usage_tracker: Any = None,
    bot_name: str = "",
    user_id: int | None = None,
    username: str | None = None,
) -> RecognitionResult:
    """Recognize a document with optional stage progress reporting.

    ``progress_callback(stage, message)`` is invoked synchronously at each
    pipeline stage (mime_detect, extract_text, ocr_scan, ocr_image,
    ai_parse, done). The callback runs in the calling thread — use
    ``asyncio.run_coroutine_threadsafe`` from async callers.

    ``usage_tracker`` (UsageTracker из hunttech_bot_common.ai) — учёт
    обращения к LLM в общем реестре расходов (bot_name/user_id/username —
    для отчёта /usage).
    """
    settings = settings or None
    _emit(progress_callback, "mime_detect", "Определяю тип файла...")
    mime_type = _guess_mime(path)
    if _is_supported_image(mime_type):
        return _recognize_image_document(
            llm, path, mime_type, caption=caption, original_name=original_name,
            settings=settings, progress_callback=progress_callback,
            usage_tracker=usage_tracker, bot_name=bot_name,
            user_id=user_id, username=username,
        )

    _emit(progress_callback, "extract_text", "Извлекаю текст из документа...")
    extracted_text = _extract_text(path, mime_type)
    if _is_pdf(mime_type, path):
        if not extracted_text.strip():
            return _recognize_pdf_scan(
                llm, path, caption=caption, original_name=original_name,
                settings=settings, progress_callback=progress_callback,
                usage_tracker=usage_tracker, bot_name=bot_name,
                user_id=user_id, username=username,
            )
        if len(extracted_text.strip()) < _PDF_TEXT_MIN_CHARS:
            # Тонкий текстовый слой: извлечённого текста мало (обычно это
            # заголовок/URL/футер печати, а содержимое — картинкой). Доснимаем
            # OCR рендера и склеиваем оба текста для LLM.
            return _recognize_pdf_mixed(
                llm,
                path,
                extracted_text,
                caption=caption,
                original_name=original_name,
                settings=settings,
                progress_callback=progress_callback,
                usage_tracker=usage_tracker, bot_name=bot_name,
                user_id=user_id, username=username,
            )

    result = _recognize_text_document(
        llm,
        path,
        mime_type,
        extracted_text,
        caption=caption,
        original_name=original_name,
        progress_callback=progress_callback,
        usage_tracker=usage_tracker, bot_name=bot_name,
        user_id=user_id, username=username,
    )
    _emit(progress_callback, "done", "Реквизиты распознаны.")
    return result

def _emit(callback: Callable[[str, str], None] | None, stage: str, message: str) -> None:
    """Invoke the optional progress callback, tolerating callback errors."""
    if callback is None:
        return
    try:
        callback(stage, message)
    except Exception:
        pass


def _recognize_text_document(
    llm: Any,
    path: Path,
    mime_type: str,
    extracted_text: str,
    *,
    caption: str = "",
    original_name: str = "",
    source_provider: str = "",
    source_model: str = "",
    progress_callback: Callable[[str, str], None] | None = None,
    usage_tracker: Any = None,
    bot_name: str = "",
    user_id: int | None = None,
    username: str | None = None,
) -> RecognitionResult:
    if llm is None:
        raise RuntimeError("Hermes plugin LLM is unavailable")
    if PluginLlmTextInput is None:
        raise RuntimeError("Hermes plugin LLM input classes are unavailable")

    _emit(progress_callback, "ai_parse", "AI разбирает реквизиты (контрагент, дата, сумма)...")
    inputs: list[Any] = [
        PluginLlmTextInput(
            text=(
                f"Original file name: {original_name or path.name}\n"
                f"MIME type: {mime_type}\n"
                f"Telegram caption: {caption or ''}\n"
                f"Extracted document text:\n{extracted_text[:12000]}\n"
            )
        )
    ]

    try:
        result = llm.complete_structured(
            instructions=INSTRUCTIONS,
            input=inputs,
            json_schema=None,
            json_mode=False,
            schema_name="hunttech_accounting_document",
            temperature=0,
            timeout=90,
            purpose="hunttech_docs_recognition",
        )
    except Exception as exc:
        _track_recognition_usage(
            usage_tracker, bot_name=bot_name, user_id=user_id,
            username=username, status="error", provider="", model="", usage=None,
        )
        raise
    _track_recognition_usage(
        usage_tracker, bot_name=bot_name, user_id=user_id,
        username=username, status="ok",
        provider=result.provider, model=result.model, usage=result.usage,
    )
    parsed = result.parsed if isinstance(result.parsed, dict) else _parse_json_fallback(result.text)
    logger.info(
        "recognition: LLM raw document_type=%r flow_type=%r confidence=%r needs_manual_review=%r (provider=%s model=%s)",
        parsed.get("document_type"), parsed.get("flow_type"),
        parsed.get("confidence"), parsed.get("needs_manual_review"),
        result.provider, result.model,
    )
    parsed = _normalize_result(parsed, hint_text=f"{original_name} {caption} {extracted_text[:2000]}")
    logger.info(
        "recognition: after _normalize_result document_type=%r flow_type=%r",
        parsed.get("document_type"), parsed.get("flow_type"),
    )
    parsed = _apply_text_hints(parsed, extracted_text)
    logger.info(
        "recognition: after _apply_text_hints document_type=%r flow_type=%r",
        parsed.get("document_type"), parsed.get("flow_type"),
    )
    provider = result.provider
    model = result.model
    if source_provider:
        provider = f"{source_provider}+{provider}"
        model = f"{source_model}+{model}" if source_model else model
    return RecognitionResult(
        parsed=parsed,
        provider=provider,
        model=model,
        raw_text=result.text,
    )

def _recognize_image_document(
    llm: Any,
    path: Path,
    mime_type: str,
    *,
    caption: str = "",
    original_name: str = "",
    settings: Any,
    progress_callback: Callable[[str, str], None] | None = None,
    usage_tracker: Any = None,
    bot_name: str = "",
    user_id: int | None = None,
    username: str | None = None,
) -> RecognitionResult:
    ocr_text, ocr_provider, ocr_model = _recognize_image_text(path, mime_type, settings, progress_callback=progress_callback)
    return _recognize_text_document(
        llm,
        path,
        mime_type,
        ocr_text,
        caption=caption,
        original_name=original_name,
        source_provider=ocr_provider,
        source_model=ocr_model,
        progress_callback=progress_callback,
        usage_tracker=usage_tracker, bot_name=bot_name,
        user_id=user_id, username=username,
    )

def _recognize_image_text(path: Path, mime_type: str, settings: Any,
                          progress_callback: Callable[[str, str], None] | None = None) -> tuple[str, str, str]:
    _emit(progress_callback, "ocr_image", "Распознаю изображение через Yandex Vision OCR...")
    yandex_error: Exception | None = None
    if getattr(settings, "yandex_vision_enabled", False):
        try:
            ocr = recognize_text_with_yandex_vision(path, mime_type, settings)
            return ocr.text, "yandex-vision-ocr", getattr(settings, "yandex_ocr_model", "page") or "page"
        except Exception as exc:
            yandex_error = exc
            if not getattr(settings, "ocr_space_enabled", False):
                raise

    if getattr(settings, "ocr_space_enabled", False):
        try:
            ocr = recognize_text_with_ocr_space(path, mime_type, settings)
            return ocr.text, "ocr-space", f"engine-{getattr(settings, 'ocr_space_engine', '2') or '2'}"
        except Exception as exc:
            if yandex_error is not None:
                raise RuntimeError(f"Yandex OCR failed: {yandex_error}; OCR.space failed: {exc}") from exc
            raise

    if yandex_error is not None:
        raise yandex_error
    raise RuntimeError("Не настроен OCR для фотографий: включите Yandex Vision OCR или OCR.space fallback.")

def _recognize_pdf_scan(
    llm: Any,
    path: Path,
    *,
    caption: str = "",
    original_name: str = "",
    settings: Any,
    progress_callback: Callable[[str, str], None] | None = None,
    usage_tracker: Any = None,
    bot_name: str = "",
    user_id: int | None = None,
    username: str | None = None,
) -> RecognitionResult:
    _emit(progress_callback, "ocr_scan", "PDF похож на скан — готовлю страницу и распознаю через OCR...")
    with tempfile.TemporaryDirectory(prefix="hunttech-docs-pdf-") as temp_dir:
        rendered = _render_first_pdf_page(path, Path(temp_dir))
        return _recognize_image_document(
            llm,
            rendered,
            "image/jpeg",
            caption=caption,
            original_name=original_name or path.name,
            settings=settings,
            progress_callback=progress_callback,
            usage_tracker=usage_tracker, bot_name=bot_name,
            user_id=user_id, username=username,
        )

def _recognize_pdf_mixed(
    llm: Any,
    path: Path,
    extracted_text: str,
    *,
    caption: str = "",
    original_name: str = "",
    settings: Any,
    progress_callback: Callable[[str, str], None] | None = None,
    usage_tracker: Any = None,
    bot_name: str = "",
    user_id: int | None = None,
    username: str | None = None,
) -> RecognitionResult:
    """PDF с тонким текстовым слоем: OCR рендера + извлечённый текст.

    Текстовый слой в таких PDF (чек check.yandex.ru и т.п.) содержит только
    заголовок/URL/футер печати, а реквизиты — картинкой. Извлечённого текста
    мало — распознаём рендер страницы через OCR и склеиваем с текстовым слоем,
    чтобы LLM получил и содержимое, и подсказку из слоя.
    """
    _emit(progress_callback, "ocr_scan", "PDF с тонким текстовым слоем — добавляю OCR страницы...")
    with tempfile.TemporaryDirectory(prefix="hunttech-docs-pdf-") as temp_dir:
        rendered = _render_first_pdf_page(path, Path(temp_dir))
        ocr_text, ocr_provider, ocr_model = _recognize_image_text(
            rendered, "image/jpeg", settings, progress_callback=progress_callback
        )
    combined = _combine_pdf_texts(extracted_text, ocr_text)
    return _recognize_text_document(
        llm,
        path,
        "application/pdf",
        extracted_text=combined,
        caption=caption,
        original_name=original_name,
        source_provider=ocr_provider,
        source_model=ocr_model,
        progress_callback=progress_callback,
        usage_tracker=usage_tracker, bot_name=bot_name,
        user_id=user_id, username=username,
    )

def _combine_pdf_texts(extracted_text: str, ocr_text: str) -> str:
    """Склейка текстового слоя и OCR-текста PDF (OCR-текст — основной)."""
    parts = [part.strip() for part in (ocr_text, extracted_text) if part and part.strip()]
    return "\n".join(parts)

def _render_first_pdf_page(path: Path, temp_dir: Path) -> Path:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError(
            "PDF похож на скан без текстового слоя, но pdftoppm не найден. "
            "Нужно установить Poppler или отправить документ как фото."
        )
    output_stem = temp_dir / "page-1"
    completed = subprocess.run(
        [
            pdftoppm,
            "-singlefile",
            "-f",
            "1",
            "-l",
            "1",
            "-r",
            "220",
            "-jpeg",
            str(path),
            str(output_stem),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    rendered = output_stem.with_suffix(".jpg")
    if completed.returncode != 0 or not rendered.is_file():
        detail = (completed.stderr or completed.stdout or "unknown pdftoppm error").strip()
        raise RuntimeError(f"Не удалось подготовить PDF-скан для vision-распознавания: {detail}")
    return rendered

def _resolve_vision_backend() -> tuple[str, str]:
    try:
        from agent.auxiliary_client import resolve_vision_provider_client
    except Exception as exc:  # pragma: no cover - Hermes import path is checked at runtime.
        raise RuntimeError("Hermes vision routing is unavailable") from exc

    provider, client, model = resolve_vision_provider_client(provider="auto")
    if client is None:
        raise RuntimeError(
            "Для фотографий не настроена доступная Hermes vision-модель. "
            "Нужно включить auxiliary.vision или подключить vision-провайдер в Hermes."
        )
    return provider or "vision", model or "auto"

def _run_vision_analysis(path: Path, prompt: str) -> str:
    try:
        return asyncio.run(_run_vision_analysis_async(path, prompt))
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" not in str(exc):
            raise
        raise RuntimeError("Image recognition must run outside an active event loop") from exc

async def _run_vision_analysis_async(path: Path, prompt: str) -> str:
    from tools.vision_tools import vision_analyze_tool

    payload = await vision_analyze_tool(str(path), prompt)
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Hermes vision returned non-JSON tool payload: {payload[:200]}") from exc
    if not result.get("success"):
        raise RuntimeError(str(result.get("error") or result.get("analysis") or "Hermes vision failed"))
    return str(result.get("analysis") or "")