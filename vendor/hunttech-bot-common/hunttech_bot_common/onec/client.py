"""1C OData HTTP client for HuntTech bots.

Provides async HTTP client with authentication, retries, batch operations,
and OData query support for 1C HTTP services.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin

import httpx

from hunttech_bot_common.exceptions import (
    AIConnectionError,
    AITimeoutError,
    AIRateLimitError,
)
from hunttech_bot_common.onec.config import OneCSettings
from hunttech_bot_common.onec.models import (
    ODataError,
    ODataResponse,
    OneCRef,
)

logger = logging.getLogger(__name__)


class OneCClientError(Exception):
    """Base exception for 1C client errors."""

    def __init__(self, message: str, status_code: int | None = None, response: httpx.Response | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class OneCAuthenticationError(OneCClientError):
    """Authentication failed (401)."""


class OneCAuthorizationError(OneCClientError):
    """Access denied (403)."""


class OneCNotFoundError(OneCClientError):
    """Resource not found (404)."""


class OneCConflictError(OneCClientError):
    """Conflict/precondition failed (409, 412)."""


class OneCServerError(OneCClientError):
    """Server error (5xx)."""


class OneCRateLimitError(OneCClientError):
    """Rate limited (429)."""

    def __init__(self, message: str, retry_after: int | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


def _parse_odata_error(response: httpx.Response) -> ODataError | None:
    """Parse OData error from response."""
    try:
        data = response.json()
        error_data = data.get("error", data)
        return ODataError(
            code=error_data.get("code", str(response.status_code)),
            message=error_data.get("message", response.text),
            target=error_data.get("target"),
            details=error_data.get("details"),
            innererror=error_data.get("innererror"),
        )
    except Exception:
        return None


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate exception for HTTP error status."""
    if response.is_success:
        return

    odata_error = _parse_odata_error(response)
    message = odata_error.message if odata_error else response.text

    if response.status_code == 401:
        raise OneCAuthenticationError(message, response.status_code, response)
    elif response.status_code == 403:
        raise OneCAuthorizationError(message, response.status_code, response)
    elif response.status_code == 404:
        raise OneCNotFoundError(message, response.status_code, response)
    elif response.status_code in (409, 412):
        raise OneCConflictError(message, response.status_code, response)
    elif response.status_code == 429:
        retry_after = None
        if response.headers.get("Retry-After"):
            try:
                retry_after = int(response.headers["Retry-After"])
            except ValueError:
                pass
        raise OneCRateLimitError(message, retry_after=retry_after, status_code=response.status_code, response=response)
    elif 500 <= response.status_code < 600:
        raise OneCServerError(message, response.status_code, response)
    else:
        raise OneCClientError(message, response.status_code, response)


class OneCClient:
    """Async HTTP client for 1C OData services.

    Features:
    - Basic authentication
    - Automatic retries with exponential backoff
    - OData query options ($filter, $select, $expand, $top, $skip, $orderby)
    - Batch operations (multipart/mixed)
    - ETag support for optimistic concurrency
    - Streaming responses for large datasets
    """

    def __init__(
        self,
        settings: OneCSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        own_client: bool = True,
    ):
        """Initialize the 1C client.

        Args:
            settings: 1C connection settings.
            http_client: Optional pre-configured httpx.AsyncClient.
            own_client: Whether this instance owns the http_client lifecycle.
        """
        self.settings = settings
        self._own_client = own_client

        auth = (settings.username, settings.password)
        auth_header = base64.b64encode(f"{settings.username}:{settings.password}".encode()).decode()

        default_headers = {
            "Authorization": f"Basic {auth_header}",
            "Accept": "application/json;odata=verbose",
            "Content-Type": "application/json;odata=verbose",
            "User-Agent": "hunttech-bot-common/onec",
        }

        if http_client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.base_url,
                auth=auth,
                headers=default_headers,
                timeout=httpx.Timeout(settings.timeout),
                verify=settings.verify_ssl,
                follow_redirects=True,
            )
        else:
            self._client = http_client
            # Update base URL and auth if needed
            self._client.base_url = httpx.URL(settings.base_url)
            self._client.auth = auth
            for k, v in default_headers.items():
                self._client.headers.setdefault(k, v)

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    async def __aenter__(self) -> OneCClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._own_client and not self._client.is_closed:
            await self._client.aclose()

    # ── Core request methods ─────────────────────────────────────

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """Execute request with retry logic."""
        last_exception: Exception | None = None
        backoff = self.settings.retry_backoff

        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await self._client.request(
                    method=method,
                    url=path,
                    params=params,
                    json=json,
                    headers=headers,
                    stream=stream,
                )
                _raise_for_status(response)
                return response
            except OneCRateLimitError as e:
                last_exception = e
                if attempt < self.settings.max_retries:
                    wait_time = e.retry_after or backoff * (2**attempt)
                    logger.warning(
                        "Rate limited, retrying in %.1fs (attempt %d/%d)",
                        wait_time,
                        attempt + 1,
                        self.settings.max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exception = e
                if attempt < self.settings.max_retries:
                    wait_time = backoff * (2**attempt)
                    logger.warning(
                        "Connection error, retrying in %.1fs (attempt %d/%d): %s",
                        wait_time,
                        attempt + 1,
                        self.settings.max_retries,
                        e,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                if isinstance(e, httpx.TimeoutException):
                    raise AITimeoutError(f"Request timeout after {self.settings.max_retries} retries") from e
                raise AIConnectionError(f"Connection failed after {self.settings.max_retries} retries") from e
            except OneCClientError:
                raise

        raise last_exception or OneCClientError("Max retries exceeded")

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        """GET request."""
        return await self._request_with_retry("GET", path, params=params, headers=headers, stream=stream)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """POST request."""
        return await self._request_with_retry("POST", path, json=json, params=params, headers=headers)

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        if_match: str | None = None,
    ) -> httpx.Response:
        """PATCH request with optional If-Match header."""
        h = headers or {}
        if if_match:
            h["If-Match"] = if_match
        return await self._request_with_retry("PATCH", path, json=json, params=params, headers=h)

    async def put(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        if_match: str | None = None,
    ) -> httpx.Response:
        """PUT request with optional If-Match header."""
        h = headers or {}
        if if_match:
            h["If-Match"] = if_match
        return await self._request_with_retry("PUT", path, json=json, params=params, headers=h)

    async def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        if_match: str | None = None,
    ) -> httpx.Response:
        """DELETE request with optional If-Match header."""
        h = headers or {}
        if if_match:
            h["If-Match"] = if_match
        return await self._request_with_retry("DELETE", path, params=params, headers=h)

    # ── OData query helpers ──────────────────────────────────────

    def _build_odata_params(
        self,
        *,
        filter_: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        top: int | None = None,
        skip: int | None = None,
        orderby: list[str] | None = None,
        count: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        """Build OData query parameters."""
        params = {}
        if filter_:
            params["$filter"] = filter_
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)
        if top is not None:
            params["$top"] = top
        if skip is not None:
            params["$skip"] = skip
        if orderby:
            params["$orderby"] = ",".join(orderby)
        if count:
            params["$count"] = "true"
        params.update(extra)
        return params

    # ── Catalog operations ───────────────────────────────────────

    async def get_catalog(
        self,
        catalog_name: str,
        *,
        filter_: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        top: int | None = None,
        skip: int | None = None,
        orderby: list[str] | None = None,
        count: bool = False,
    ) -> ODataResponse:
        """Get catalog entries with OData query options."""
        params = self._build_odata_params(
            filter_=filter_,
            select=select,
            expand=expand,
            top=top,
            skip=skip,
            orderby=orderby,
            count=count,
        )
        response = await self.get(catalog_name, params=params)
        return ODataResponse.model_validate(response.json())

    async def get_catalog_by_key(
        self,
        catalog_name: str,
        key: str,
        *,
        select: list[str] | None = None,
        expand: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Get single catalog entry by key."""
        path = f"{catalog_name}(guid'{key}')"
        params = self._build_odata_params(select=select, expand=expand)
        try:
            response = await self.get(path, params=params)
            return response.json()
        except OneCNotFoundError:
            return None

    async def create_catalog_entry(
        self,
        catalog_name: str,
        data: dict[str, Any],
        *,
        if_none_match: bool = False,
    ) -> OneCRef:
        """Create new catalog entry."""
        headers = {}
        if if_none_match:
            headers["If-None-Match"] = "*"
        response = await self.post(catalog_name, json=data, headers=headers)
        result = response.json()
        return OneCRef.model_validate(result)

    async def update_catalog_entry(
        self,
        catalog_name: str,
        key: str,
        data: dict[str, Any],
        *,
        etag: str | None = None,
    ) -> OneCRef:
        """Update catalog entry (PATCH)."""
        path = f"{catalog_name}(guid'{key}')"
        response = await self.patch(path, json=data, if_match=etag)
        result = response.json()
        return OneCRef.model_validate(result)

    async def delete_catalog_entry(
        self,
        catalog_name: str,
        key: str,
        *,
        etag: str | None = None,
    ) -> None:
        """Delete catalog entry."""
        path = f"{catalog_name}(guid'{key}')"
        await self.delete(path, if_match=etag)

    # ── Document operations ──────────────────────────────────────

    async def get_document(
        self,
        document_name: str,
        *,
        filter_: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        top: int | None = None,
        skip: int | None = None,
        orderby: list[str] | None = None,
        count: bool = False,
    ) -> ODataResponse:
        """Get document entries with OData query options."""
        params = self._build_odata_params(
            filter_=filter_,
            select=select,
            expand=expand,
            top=top,
            skip=skip,
            orderby=orderby,
            count=count,
        )
        response = await self.get(document_name, params=params)
        return ODataResponse.model_validate(response.json())

    async def get_document_by_key(
        self,
        document_name: str,
        key: str,
        *,
        select: list[str] | None = None,
        expand: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Get single document by key."""
        path = f"{document_name}(guid'{key}')"
        params = self._build_odata_params(select=select, expand=expand)
        try:
            response = await self.get(path, params=params)
            return response.json()
        except OneCNotFoundError:
            return None

    async def create_document(
        self,
        document_name: str,
        data: dict[str, Any],
        *,
        post: bool = False,
    ) -> OneCRef:
        """Create new document."""
        if post:
            data["Posted"] = True
        response = await self.post(document_name, json=data)
        result = response.json()
        return OneCRef.model_validate(result)

    async def post_document(
        self,
        document_name: str,
        key: str,
        *,
        etag: str | None = None,
    ) -> OneCRef:
        """Post (approve) a document."""
        path = f"{document_name}(guid'{key}')/Post"
        response = await self.post(path, json={}, if_match=etag)
        result = response.json()
        return OneCRef.model_validate(result)

    async def unpost_document(
        self,
        document_name: str,
        key: str,
        *,
        etag: str | None = None,
    ) -> OneCRef:
        """Unpost (unapprove) a document."""
        path = f"{document_name}(guid'{key}')/Unpost"
        response = await self.post(path, json={}, if_match=etag)
        result = response.json()
        return OneCRef.model_validate(result)

    # ── Batch operations ─────────────────────────────────────────

    async def execute_batch(
        self,
        operations: list[dict[str, Any]],
        *,
        changeset: bool = True,
    ) -> dict[str, Any]:
        """Execute OData batch request.

        Args:
            operations: List of operation dicts with keys:
                - method: HTTP method (GET, POST, PATCH, DELETE)
                - path: Relative path
                - data: JSON payload (for POST/PATCH)
                - headers: Additional headers
                - id: Optional operation ID for reference
            changeset: Whether to wrap in a changeset (atomic).

        Returns:
            Batch response with results for each operation.
        """
        boundary = f"batch_{int(time.time() * 1000)}"
        changeset_boundary = f"changeset_{int(time.time() * 1000)}"

        body_parts = [f"--{boundary}"]

        if changeset:
            body_parts.append(f"--{changeset_boundary}")

        for i, op in enumerate(operations):
            method = op.get("method", "GET").upper()
            path = op.get("path", "")
            data = op.get("data")
            op_headers = op.get("headers", {})

            # Build individual request
            req_lines = [f"{method} {path} HTTP/1.1"]
            req_lines.append("Content-Type: application/json;odata=verbose")
            req_lines.append("Accept: application/json;odata=verbose")
            for k, v in op_headers.items():
                req_lines.append(f"{k}: {v}")
            if op.get("id"):
                req_lines.append(f"Content-ID: {op['id']}")
            req_lines.append("")

            if data is not None:
                import json
                req_lines.append(json.dumps(data, ensure_ascii=False))

            body_parts.append("\r\n".join(req_lines))
            body_parts.append(f"--{changeset_boundary}")

        body_parts.append(f"--{boundary}--")
        body = "\r\n".join(body_parts) + "\r\n"

        headers = {
            "Content-Type": f"multipart/mixed; boundary={boundary}",
            "Accept": "multipart/mixed",
        }

        response = await self.post("$batch", json=None, headers=headers)
        # Parse multipart response
        return await self._parse_batch_response(response, boundary)

    async def _parse_batch_response(
        self,
        response: httpx.Response,
        boundary: str,
    ) -> dict[str, Any]:
        """Parse multipart batch response."""
        content_type = response.headers.get("Content-Type", "")
        if "multipart/mixed" not in content_type:
            return {"results": [response.json()], "errors": []}

        # Simple multipart parsing
        body = response.text
        parts = body.split(f"--{boundary}")
        results = []
        errors = []

        for part in parts:
            if not part.strip() or part.strip() == "--":
                continue
            try:
                # Extract HTTP response from part
                lines = part.strip().split("\r\n")
                status_line = lines[0] if lines else ""
                if "200" in status_line or "201" in status_line or "204" in status_line:
                    # Find JSON body
                    json_start = part.find("{")
                    if json_start >= 0:
                        import json
                        results.append(json.loads(part[json_start:]))
                else:
                    errors.append(part)
            except Exception as e:
                logger.warning("Failed to parse batch part: %s", e)
                errors.append(str(e))

        return {"results": results, "errors": errors}

    # ── Streaming / pagination ───────────────────────────────────

    async def iter_catalog(
        self,
        catalog_name: str,
        *,
        filter_: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        batch_size: int = 100,
        orderby: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate over catalog entries with automatic pagination."""
        skip = 0
        while True:
            resp = await self.get_catalog(
                catalog_name,
                filter_=filter_,
                select=select,
                expand=expand,
                top=batch_size,
                skip=skip,
                orderby=orderby,
            )
            items = resp.value
            if not items:
                break
            for item in items:
                yield item
            if len(items) < batch_size:
                break
            skip += batch_size

    async def iter_document(
        self,
        document_name: str,
        *,
        filter_: str | None = None,
        select: list[str] | None = None,
        expand: list[str] | None = None,
        batch_size: int = 100,
        orderby: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate over document entries with automatic pagination."""
        skip = 0
        while True:
            resp = await self.get_document(
                document_name,
                filter_=filter_,
                select=select,
                expand=expand,
                top=batch_size,
                skip=skip,
                orderby=orderby,
            )
            items = resp.value
            if not items:
                break
            for item in items:
                yield item
            if len(items) < batch_size:
                break
            skip += batch_size

    # ── Metadata / service info ──────────────────────────────────

    async def get_service_metadata(self) -> dict[str, Any]:
        """Get OData service metadata ($metadata)."""
        response = await self.get("$metadata", headers={"Accept": "application/xml"})
        return {"xml": response.text}

    async def health_check(self) -> dict[str, Any]:
        """Check 1C service availability."""
        try:
            start = time.perf_counter()
            response = await self.get("Catalogs", params={"$top": 1, "$count": "true"})
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "status": "connected",
                "latency_ms": round(latency_ms, 2),
                "catalogs_available": True,
            }
        except Exception as e:
            logger.warning("1C health check failed: %s", e)
            return {
                "status": "error",
                "error": str(e),
                "catalogs_available": False,
            }


@asynccontextmanager
async def create_onec_client(
    settings: OneCSettings,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[OneCClient]:
    """Create and manage 1C client lifecycle."""
    client = OneCClient(settings, http_client=http_client, own_client=http_client is None)
    try:
        yield client
    finally:
        await client.close()


__all__ = [
    "OneCClient",
    "OneCClientError",
    "OneCAuthenticationError",
    "OneCAuthorizationError",
    "OneCNotFoundError",
    "OneCConflictError",
    "OneCServerError",
    "OneCRateLimitError",
    "create_onec_client",
]