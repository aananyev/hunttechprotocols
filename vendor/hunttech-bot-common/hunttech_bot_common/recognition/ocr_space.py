"""OCR.space fallback integration for HRM HuntTech document scans."""

from __future__ import annotations

import json
import mimetypes
import tempfile
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_FREE_FILE_BYTES = 950 * 1024


@dataclass(frozen=True)
class OcrSpaceResult:
    text: str
    raw: dict[str, Any]


def recognize_text_with_ocr_space(path: Path, mime_type: str, settings: Any) -> OcrSpaceResult:
    if not getattr(settings, "ocr_space_enabled", False):
        raise RuntimeError("OCR.space fallback выключен в настройках hunttech_docs.")
    api_key = getattr(settings, "ocr_space_api_key", "")
    if not api_key:
        raise RuntimeError("OCR.space fallback не настроен: не указан HUNTTECH_DOCS_OCR_SPACE_API_KEY.")

    with tempfile.TemporaryDirectory(prefix="hunttech-docs-ocr-space-") as temp_dir:
        upload_path = _prepare_upload_file(path, mime_type, Path(temp_dir))
        raw = _post_to_ocr_space(upload_path, api_key, settings)
    if raw.get("IsErroredOnProcessing"):
        errors = raw.get("ErrorMessage") or raw.get("ErrorDetails") or "OCR.space processing error"
        raise RuntimeError(f"OCR.space вернул ошибку: {errors}")

    parts: list[str] = []
    for result in raw.get("ParsedResults") or []:
        if isinstance(result, dict):
            text = result.get("ParsedText")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    text = "\n".join(parts).strip()
    if not text:
        raise RuntimeError("OCR.space не нашёл текст на изображении.")
    return OcrSpaceResult(text=text, raw=raw)


def _prepare_upload_file(path: Path, mime_type: str, temp_dir: Path) -> Path:
    if path.stat().st_size <= MAX_FREE_FILE_BYTES:
        return path
    if not (mime_type or "").startswith("image/"):
        raise RuntimeError(
            "OCR.space free API принимает небольшие файлы; PDF-скан нужно сначала "
            "отрендерить в изображение или уменьшить файл."
        )
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Для уменьшения фото под OCR.space нужен Pillow.") from exc

    target = temp_dir / "ocr-space-upload.jpg"
    with Image.open(path) as image:
        image = image.convert("RGB")
        quality = 82
        max_side = max(image.size)
        while True:
            image.save(target, format="JPEG", quality=quality, optimize=True)
            if target.stat().st_size <= MAX_FREE_FILE_BYTES:
                return target
            if quality > 50:
                quality -= 10
                continue
            if max_side <= 1200:
                break
            max_side = int(max_side * 0.82)
            image.thumbnail((max_side, max_side))
            quality = 82
    raise RuntimeError("Не удалось уменьшить фото до лимита OCR.space free API.")


def _post_to_ocr_space(path: Path, api_key: str, settings: Any) -> dict[str, Any]:
    boundary = f"----hunttech-docs-{uuid.uuid4().hex}"
    fields = {
        "apikey": api_key,
        "language": getattr(settings, "ocr_space_language", "rus") or "rus",
        "OCREngine": getattr(settings, "ocr_space_engine", "2") or "2",
        "isOverlayRequired": "false",
        "detectOrientation": "true",
        "scale": "true",
    }
    body = _multipart_body(boundary, fields, path)
    request = urllib.request.Request(
        getattr(settings, "ocr_space_endpoint"),
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_data = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OCR.space вернул ошибку HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OCR.space недоступен: {exc.reason}") from exc
    try:
        value = json.loads(response_data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OCR.space вернул не JSON: {response_data[:500]}") from exc
    return value if isinstance(value, dict) else {}


def _multipart_body(boundary: str, fields: dict[str, str], path: Path) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks)
