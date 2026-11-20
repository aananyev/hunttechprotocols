"""Yandex Vision OCR integration for HRM HuntTech document scans."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunttech_bot_common.security import validate_url


@dataclass(frozen=True)
class YandexOcrResult:
    text: str
    raw: dict[str, Any]


def recognize_text_with_yandex_vision(path: Path, mime_type: str, settings: Any) -> YandexOcrResult:
    if not getattr(settings, "yandex_vision_enabled", False):
        raise RuntimeError("Yandex Vision OCR выключен в настройках hunttech_docs.")
    if not getattr(settings, "yandex_folder_id", ""):
        raise RuntimeError("Yandex Vision OCR не настроен: не указан folder_id каталога Yandex Cloud.")
    if not getattr(settings, "yandex_api_key", "") and not getattr(settings, "yandex_iam_token", ""):
        raise RuntimeError("Yandex Vision OCR не настроен: не указан API key или IAM token.")

    yandex_mime = _to_yandex_mime_type(mime_type, path)
    payload = {
        "mimeType": yandex_mime,
        "languageCodes": ["ru", "en"],
        "model": getattr(settings, "yandex_ocr_model", "page") or "page",
        "content": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
    headers = {
        "Content-Type": "application/json",
        "x-folder-id": getattr(settings, "yandex_folder_id"),
        "x-data-logging-enabled": "true" if getattr(settings, "yandex_data_logging_enabled", False) else "false",
    }
    api_key = getattr(settings, "yandex_api_key", "")
    if api_key:
        headers["Authorization"] = f"Api-Key {api_key}"
    else:
        headers["Authorization"] = f"Bearer {getattr(settings, 'yandex_iam_token')}"

    # Валидация URL endpoint
    endpoint = validate_url(getattr(settings, "yandex_ocr_endpoint"), allow_private=True)

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_data = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Yandex Vision OCR вернул ошибку HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Yandex Vision OCR недоступен: {exc.reason}") from exc

    try:
        raw = json.loads(response_data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Yandex Vision OCR вернул не JSON: {response_data[:500]}") from exc

    text = _extract_text(raw)
    if not text.strip():
        raise RuntimeError("Yandex Vision OCR не нашёл текст на изображении.")
    return YandexOcrResult(text=text, raw=raw)


def _to_yandex_mime_type(mime_type: str, path: Path) -> str:
    normalized = (mime_type or "").lower()
    suffix = path.suffix.lower()
    if normalized == "image/jpeg" or suffix in {".jpg", ".jpeg"}:
        return "JPEG"
    if normalized == "image/png" or suffix == ".png":
        return "PNG"
    if normalized == "application/pdf" or suffix == ".pdf":
        return "PDF"
    raise RuntimeError(f"Yandex Vision OCR не поддерживает тип файла для распознавания: {mime_type or suffix}")


def _extract_text(raw: dict[str, Any]) -> str:
    result = raw.get("result")
    candidates: list[str] = []

    if isinstance(result, dict):
        _collect_text_annotation(result.get("textAnnotation"), candidates)
        _collect_text_annotation(result.get("text_annotation"), candidates)
    elif isinstance(result, list):
        for page in result:
            if isinstance(page, dict):
                _collect_text_annotation(page.get("textAnnotation"), candidates)
                _collect_text_annotation(page.get("text_annotation"), candidates)
                _collect_line_text(page, candidates)

    if not candidates:
        _collect_line_text(raw, candidates)
    return "\n".join(part for part in candidates if part).strip()


def _collect_text_annotation(annotation: Any, candidates: list[str]) -> None:
    if not isinstance(annotation, dict):
        return
    full_text = annotation.get("fullText") or annotation.get("full_text")
    if isinstance(full_text, str) and full_text.strip():
        candidates.append(full_text.strip())
        return
    _collect_line_text(annotation, candidates)


def _collect_line_text(value: Any, candidates: list[str]) -> None:
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            candidates.append(text.strip())
        for child in value.values():
            _collect_line_text(child, candidates)
    elif isinstance(value, list):
        for item in value:
            _collect_line_text(item, candidates)
