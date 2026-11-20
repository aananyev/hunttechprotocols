"""Document recognition and OCR for HuntTech bots.

Provides a unified interface for recognizing accounting documents:
text extraction from PDFs, OCR via multiple backends (Yandex Vision,
OCR.space, Hermes vision), and AI-powered structured data extraction
using configurable prompts and post-processing hints.

Usage:
    from hunttech_bot_common.recognition import recognize_document, RecognitionResult

    result = recognize_document(llm, path, caption="...", original_name="...", settings=...)
    # result.parsed -> {"flow_type": "PRIMARY", "document_type": "INVOICE", ...}
"""

from hunttech_bot_common.recognition.engine import (
    recognize_document,
    _recognize_image_document,
    _recognize_image_text,
    _recognize_pdf_scan,
    _recognize_text_document,
    _render_first_pdf_page,
    _resolve_vision_backend,
    _run_vision_analysis,
    _run_vision_analysis_async,
)
from hunttech_bot_common.recognition.schemas import (
    DOCUMENT_SCHEMA,
    INSTRUCTIONS,
    RecognitionResult,
    _extract_json_object,
    _parse_json_fallback,
    _guess_mime,
    _is_pdf,
    _is_supported_image,
    _extract_text,
    _normalize_result,
)
from hunttech_bot_common.recognition.text_hints import (
    _apply_text_hints,
    _extract_document_number_date,
    _extract_inn,
    _extract_named_value,
    _extract_party_by_label,
    _extract_first_legal_entity,
    _extract_primary_party,
    _extract_receipt_organization,
    _extract_total_amount,
    _is_weak_receipt_counterparty,
    _is_weak_receipt_organization,
    _looks_like_receipt,
    _date_from_ddmmyyyy,
    _date_from_russian_month, _normalize_enum, _normalize_confidence
)

__all__ = [
    "recognize_document",
    "RecognitionResult",
    "DOCUMENT_SCHEMA",
    "INSTRUCTIONS",
    # helpers
    "_apply_text_hints",
    "_looks_like_receipt",
    "_extract_text",
    "_normalize_result",
    "_parse_json_fallback",
    "_extract_json_object",
    "_guess_mime",
    "_is_pdf",
    "_is_supported_image",
    "_recognize_image_document",
    "_recognize_image_text",
    "_recognize_pdf_scan",
    "_recognize_text_document",
    "_render_first_pdf_page",
    "_resolve_vision_backend",
    "_run_vision_analysis",
    "_run_vision_analysis_async",
    "_normalize_enum",
    "_normalize_confidence",
]
