"""
AI module — OpenAI-compatible async HTTP client with retry logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import httpx

from hunttech_bot_common.exceptions import (
    AIAuthenticationError,
    AIConnectionError,
    AIInvalidResponseError,
    AIRateLimitError,
    AISchemaValidationError,
    AITimeoutError,
)

from hunttech_bot_common.ai.usage import (
    UsageRecord,
    UsageTracker,
    estimate_cost,
    format_usage_report,
    usage_period_from_args,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

@dataclass
class AIResponse:
    """Response from an AI completion request."""

    content: str
    duration_ms: float
    usage: dict[str, int] = field(default_factory=dict)


def strip_json_markdown(text: str) -> str:
    """Extract JSON from markdown code blocks if present.

    Handles ```json ... ``` fences and ``` ... ``` blocks.
    Returns the cleaned JSON string.
    """
    text = text.strip()
    # Try ```json ... ```
    match = re.search(
        r"```(?:json)\s*\n?(.*?)```", text, re.DOTALL | re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    # Try ``` ... ``` (no language)
    match = re.search(r"```\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def parse_structured_response(
    raw_response: str,
    schema: type[T],
    allow_markdown_fence: bool = True,
) -> T:
    """Parse a structured JSON response from an LLM into a given schema type.

    Args:
        raw_response: The raw text response from the AI.
        schema: A dataclass type to parse into.
        allow_markdown_fence: If True, strip markdown code fences before parsing.

    Returns:
        An instance of the schema type.

    Raises:
        AISchemaValidationError: If parsing or validation fails.
    """
    try:
        text = raw_response
        if allow_markdown_fence:
            text = strip_json_markdown(raw_response)
        data = json.loads(text)
        # If schema is a dataclass, construct it from kwargs
        if hasattr(schema, "__dataclass_fields__"):
            return schema(**data)
        # Otherwise assume it's something like pydantic or just pass through
        return schema(data)  # type: ignore[call-arg]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AISchemaValidationError(
            f"Failed to parse response into {schema.__name__}: {exc}"
        ) from exc


class AIClient:
    """OpenAI-compatible async HTTP client for AI completions.

    Supports OpenRouter headers and retry logic with exponential backoff.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        provider: str = "openai",
        default_timeout: int = 120,
        *,
        user_id: int | None = None,
        username: str | None = None,
        bot_name: str = "",
        usage_tracker: UsageTracker | None = None,
        ai_source: str = "",
        proxy: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.default_timeout = default_timeout
        # Proxy for HTTP requests (e.g. "http://user:pass@host:port")
        self.proxy = proxy
        # ── Учёт обращений к нейросети (08.2026) ────────────────────
        # user_id/username — владелец активного ключа (чьи креды
        # использованы); usage_tracker — общий реестр всех ботов
        # (ai/usage.py); ai_source — «личные» | «админ (.env)».
        self.user_id = user_id
        self.username = username or ""
        self.bot_name = bot_name
        self.usage_tracker = usage_tracker
        self.ai_source = ai_source

    def _track_usage(
        self,
        status: str,
        response: AIResponse | None = None,
        task: str | None = None,
    ) -> None:
        """Записать обращение в UsageTracker (ok/error). Не роняет запрос."""
        if self.usage_tracker is None:
            return
        usage = (response.usage if response is not None else {}) or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        record = UsageRecord(
            bot_name=self.bot_name,
            user_id=self.user_id,
            username=self.username or "",
            provider=self.provider,
            model=self.model,
            task=task or "unknown",
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            duration_ms=(response.duration_ms if response is not None else 0.0),
            cost_usd=estimate_cost(self.model, prompt_tokens, completion_tokens),
            source=self.ai_source,
        )
        try:
            self.usage_tracker.append(record)
        except Exception as e:  # noqa: BLE001 — учёт не должен ронять запрос
            logger.warning("ai usage track failed: %s", e)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[T] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra_body: dict[str, Any] | None = None,
        task: str | None = None,
    ) -> AIResponse:
        """Send a completion request to the AI provider.

        Uses retry logic with exponential backoff for transient failures.

        Args:
            system_prompt: The system-level prompt.
            user_prompt: The user prompt/message.
            response_schema: Optional schema type to parse the response into.
            temperature: Optional temperature override.
            max_tokens: Optional max tokens override.
            timeout: Optional per-call timeout override (seconds).
                Falls back to default_timeout from constructor when None.
            extra_body: Optional extra JSON body fields merged into the request
                (e.g. {"thinking": {"type": "disabled"}} for DeepSeek reasoning
                models, 08.2026).
            task: Бизнес-задача для учёта токенов (08.2026) — имя AI-функции
                (detect_intent, build_vacancy_description, …). Попадает в
                отчёт /usage в разрез «По задачам».

        Returns:
            An AIResponse with content, duration_ms, and usage.

        Raises:
            AIConnectionError: On connection failures.
            AIAuthenticationError: On auth failures.
            AIRateLimitError: On rate limiting.
            AITimeoutError: On timeout.
            AIInvalidResponseError: On invalid responses.
            AISchemaValidationError: On schema validation failures.
        """
        last_exception: Exception | None = None
        delay = 1.0
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._do_complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    extra_body=extra_body,
                )
                self._track_usage(status="ok", response=response, task=task)
                return response
            except (AIConnectionError, AIRateLimitError, AITimeoutError) as exc:
                last_exception = exc
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay *= 2  # exponential backoff
                else:
                    self._track_usage(status="error", task=task)
                    raise
            except (AIAuthenticationError, AIInvalidResponseError, AISchemaValidationError):
                # Non-retryable errors
                self._track_usage(status="error", task=task)
                raise

    async def _do_complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AIResponse:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # OpenRouter headers
        if self.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/hunttech"
            headers["X-Title"] = "HuntTech Bot"

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if extra_body:
            body.update(extra_body)

        request_timeout = self.default_timeout if timeout is None else timeout
        start_time = time.monotonic()

        try:
            # Proxy support: pass proxy to AsyncClient if configured
            client_kwargs = {"timeout": httpx.Timeout(request_timeout)}
            if self.proxy:
                client_kwargs["proxy"] = self.proxy
            # Auto-append /chat/completions для OpenAI-совместимых эндпоинтов,
            # если не указан полный URL (для всех провайдеров, не только OpenRouter)
            endpoint = self.endpoint
            if not endpoint.rstrip("/").endswith("/chat/completions"):
                endpoint = endpoint.rstrip("/") + "/chat/completions"
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise AITimeoutError(
                f"AI request timed out after {request_timeout}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise AIConnectionError(
                f"Failed to connect to {self.endpoint}: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AIConnectionError(f"HTTP error during request: {exc}") from exc

        duration_ms = (time.monotonic() - start_time) * 1000

        if response.status_code == 401:
            raise AIAuthenticationError("Invalid API key or authentication failed")
        elif response.status_code == 429:
            raise AIRateLimitError("Rate limited by AI provider")
        elif response.status_code == 402:
            logger.error("AI provider balance insufficient (402): %s — switching to fallback chain", response.text[:200])
            raise AIRateLimitError("AI provider balance insufficient (402): switching to fallback chain")
        elif response.status_code >= 500:
            raise AIConnectionError(
                f"AI provider returned {response.status_code}: {response.text[:200]}"
            )
        elif response.status_code != 200:
            raise AIInvalidResponseError(
                f"Unexpected status code {response.status_code}: {response.text[:200]}"
            )

        try:
            # Some providers (notably NVIDIA Nemotron via OpenRouter) prepend
            # whitespace/newlines before the JSON body. Strip to make robust.
            data = json.loads(response.text.strip())
        except json.JSONDecodeError as exc:
            raise AIInvalidResponseError(
                f"Invalid JSON in response: {exc}"
            ) from exc

        # Some providers (OpenRouter free tier) return HTTP 200 with an
        # error body when upstream provider is overloaded/unavailable.
        # Detect and re-raise as AIConnectionError so fallback triggers.
        if isinstance(data, dict) and "error" in data and "choices" not in data:
            err_msg = data["error"].get("message", "Unknown error")
            err_code = data["error"].get("code", response.status_code)
            logger.warning("AI provider returned error in 200 body: %s (code=%s)", err_msg, err_code)
            raise AIConnectionError(
                f"AI provider error ({err_code}): {err_msg}"
            )

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIInvalidResponseError(
                f"Unexpected response structure: {exc}"
            ) from exc

        usage: dict[str, int] = data.get("usage", {})

        return AIResponse(content=content, duration_ms=duration_ms, usage=usage)


class MockAIClient:
    """Mock AI client for testing purposes.

    Returns a fixed response without making any HTTP calls.
    """

    def __init__(self, response_text: str = "mock response") -> None:
        self.response_text = response_text

    async def complete(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        response_schema: type[T] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra_body: dict[str, Any] | None = None,
        task: str | None = None,
    ) -> AIResponse:
        """Return a mock response immediately."""
        _ = system_prompt, user_prompt, temperature, max_tokens, timeout, extra_body  # mark as used
        _ = task  # совместимость с сигнатурой AIClient.complete (учёт 0.6.0)
        content = self.response_text
        if response_schema is not None:
            try:
                content = strip_json_markdown(self.response_text)
                data = json.loads(content)
                if hasattr(response_schema, "__dataclass_fields__"):
                    response_schema(**data)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                from hunttech_bot_common.exceptions import AISchemaValidationError
                raise AISchemaValidationError(
                    f"Mock response failed schema validation: {exc}"
                ) from exc
        return AIResponse(content=content, duration_ms=0.0, usage={})


# ═══════════════════════════════════════════════════════════════════
# FALLBACK AI CLIENT — основной + резервная модель с уведомлением
# ═══════════════════════════════════════════════════════════════════

class FallbackAIClient:
    """Wrapper around two AIClients: primary and fallback.

    Tries primary first; on any error, switches to fallback and notifies user.
    """

    def __init__(
        self,
        primary: AIClient,
        fallback: AIClient,
        notify_func: Callable[[str], Callable[[str], Awaitable[None]]] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.notify_func = notify_func
        self._using_fallback = False

    @property
    def using_fallback(self) -> bool:
        return self._using_fallback

    async def _notify(self, message: str) -> None:
        if self.notify_func:
            try:
                await self.notify_func(message)
            except Exception as e:  # noqa: BLE001
                logger.warning("Fallback notification failed: %s", e)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[T] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        extra_body: dict[str, Any] | None = None,
        task: str | None = None,
    ) -> AIResponse:
        """Try primary; on failure, use fallback and notify."""
        try:
            response = await self.primary.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_schema=response_schema,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                extra_body=extra_body,
                task=task,
            )
            # If we were using fallback before, notify recovery
            if self._using_fallback:
                self._using_fallback = False
                await self._notify("✅ Основная модель восстановлена — переключились обратно на DeepSeek")
            return response
        except Exception as e:
            # Primary failed — try fallback
            if not self._using_fallback:
                self._using_fallback = True
                primary_model = self.primary.model
                fallback_model = self.fallback.model
                await self._notify(
                    f"⚠️ Основная модель ({primary_model}) недоступна: {type(e).__name__}\n"
                    f"🔄 Переключение на резервную модель: {fallback_model}"
                )
            # Try fallback
            try:
                response = await self.fallback.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=response_schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    extra_body=extra_body,
                    task=task,
                )
                return response
            except Exception as fallback_e:
                # Both failed
                self._using_fallback = False
                await self._notify(
                    f"❌ Обе модели недоступны.\n"
                    f"Основная ({self.primary.model}): {type(e).__name__}: {e}\n"
                    f"Резервная ({self.fallback.model}): {type(fallback_e).__name__}: {fallback_e}"
                )
                raise


def create_fallback_ai_client(
    primary_endpoint: str,
    primary_api_key: str,
    primary_model: str,
    fallback_endpoint: str,
    fallback_api_key: str,
    fallback_model: str,
    *,
    primary_provider: str | None = None,
    fallback_provider: str | None = None,
    default_timeout: int = 120,
    user_id: int | None = None,
    username: str | None = None,
    bot_name: str = "",
    usage_tracker: UsageTracker | None = None,
    ai_source: str = "",
    notify_func: Callable[[str], Awaitable[None]] | None = None,
    proxy: str | None = None,
) -> FallbackAIClient:
    """Factory to create a FallbackAIClient with primary and fallback configs.

    Each client can use a different provider (e.g. primary=openai/DeepSeek,
    fallback=openrouter/NVIDIA). If provider not specified, it's auto-detected
    from endpoint URL.
    """
    if primary_provider is None:
        primary_provider = "openrouter" if "openrouter" in primary_endpoint.lower() else "openai"
    if fallback_provider is None:
        fallback_provider = "openrouter" if "openrouter" in fallback_endpoint.lower() else "openai"

    primary = AIClient(
        endpoint=primary_endpoint,
        api_key=primary_api_key,
        model=primary_model,
        provider=primary_provider,
        default_timeout=default_timeout,
        user_id=user_id,
        username=username,
        bot_name=bot_name,
        usage_tracker=usage_tracker,
        ai_source=ai_source,
        proxy=proxy,
    )
    fallback = AIClient(
        endpoint=fallback_endpoint,
        api_key=fallback_api_key,
        model=fallback_model,
        provider=fallback_provider,
        default_timeout=default_timeout,
        user_id=user_id,
        username=username,
        bot_name=bot_name,
        usage_tracker=usage_tracker,
        ai_source=ai_source,
        proxy=proxy,
    )
    return FallbackAIClient(primary, fallback, notify_func)


class LLMFallbackWrapper:
    """Прозрачная обёртка над LLM-объектом (например, ctx.llm из Hermes-плагина) с fallback.

    Делегирует вызовы complete() и complete_structured() к primary_llm.
    При любой ошибке (auth/rate limit/connection) переключается на fallback_llm
    и уведомляет администратора через notify_func.

    Использование в плагинах:
        from hunttech_bot_common.ai import LLMFallbackWrapper, create_fallback_ai_client
        fallback_llm = create_fallback_ai_client(
            primary_endpoint=...,
            primary_api_key=...,
            primary_model=...,
            fallback_endpoint=...,
            fallback_api_key=...,
            fallback_model=...,
            bot_name="docs-bot",
            notify_func=async_notify,
        )
        # Оборачиваем ctx.llm (PluginLlm) нашей обёрткой:
        wrapped_llm = LLMFallbackWrapper(primary_llm=ctx.llm, fallback_llm=fallback_llm, ...)
        # Передаём wrapped_llm в recognize_document вместо ctx.llm.
    """

    def __init__(
        self,
        primary_llm: Any,
        fallback_llm: "FallbackAIClient | AIClient",
        notify_func: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.primary_llm = primary_llm
        self.fallback_llm = fallback_llm
        self.notify_func = notify_func

    async def _notify(self, message: str) -> None:
        if self.notify_func is None:
            return
        try:
            await self.notify_func(message)
        except Exception:
            logger.warning("notify_func failed: %s", message)

    async def _try_primary(self, method_name: str, *args, **kwargs):
        method = getattr(self.primary_llm, method_name, None)
        if method is None:
            raise AttributeError(f"primary_llm has no method {method_name!r}")
        return await method(*args, **kwargs) if asyncio.iscoroutinefunction(method) else method(*args, **kwargs)

    async def _try_fallback(self, method_name: str, *args, **kwargs):
        method = getattr(self.fallback_llm, method_name, None)
        if method is None:
            # FallbackAIClient использует complete() → проксируем в complete
            if method_name in ("complete_structured", "acomplete_structured"):
                return await self.fallback_llm.complete(*args, **kwargs)
            if method_name in ("acomplete",):
                return await self.fallback_llm.complete(*args, **kwargs)
            raise AttributeError(f"fallback_llm has no method {method_name!r}")
        return await method(*args, **kwargs) if asyncio.iscoroutinefunction(method) else method(*args, **kwargs)

    async def _complete_async(self, *args, **kwargs):
        """Прокси для primary_llm.complete() / fallback_llm.complete() (async версия с fallback)."""
        try:
            return await self._try_primary("complete", *args, **kwargs)
        except Exception as primary_exc:
            primary_label = _label_llm(self.primary_llm)
            fallback_label = _label_llm(self.fallback_llm)
            await self._notify(
                f"⚠️ Основная LLM ({primary_label}) недоступна: {type(primary_exc).__name__}: {primary_exc}\n"
                f"🔄 Переключение на резервную: {fallback_label}"
            )
            try:
                return await self._try_fallback("complete", *args, **kwargs)
            except Exception as fallback_exc:
                await self._notify(
                    f"❌ Обе модели недоступны.\n"
                    f"Основная ({primary_label}): {type(primary_exc).__name__}: {primary_exc}\n"
                    f"Резервная ({fallback_label}): {type(fallback_exc).__name__}: {fallback_exc}"
                )
                raise

    def complete(self, *args, **kwargs):
        """Синхронный прокси для primary_llm.complete().

        Нужен для обратной совместимости с кодом, который вызывает
        llm.complete() без await. Вызывает primary напрямую без fallback.
        """
        method = getattr(self.primary_llm, "complete", None)
        if method is None:
            raise AttributeError("primary_llm has no method 'complete'")
        return method(*args, **kwargs)

    async def acomplete(self, *args, **kwargs):
        return await self._complete_async(*args, **kwargs)

    async def _complete_structured_async(self, *args, **kwargs):
        """Прокси для primary_llm.complete_structured() → fallback (async версия с fallback)."""
        try:
            return await self._try_primary("complete_structured", *args, **kwargs)
        except Exception as primary_exc:
            primary_label = _label_llm(self.primary_llm)
            fallback_label = _label_llm(self.fallback_llm)
            await self._notify(
                f"⚠️ Основная LLM ({primary_label}) недоступна: {type(primary_exc).__name__}: {primary_exc}\n"
                f"🔄 Переключение на резервную: {fallback_label}"
            )
            try:
                return await self._try_fallback("complete_structured", *args, **kwargs)
            except Exception as fallback_exc:
                await self._notify(
                    f"❌ Обе модели недоступны.\n"
                    f"Основная ({primary_label}): {type(primary_exc).__name__}: {primary_exc}\n"
                    f"Резервная ({fallback_label}): {type(fallback_exc).__name__}: {fallback_exc}"
                )
                raise

    def complete_structured(self, *args, **kwargs):
        """Синхронный прокси для primary_llm.complete_structured().

        Нужен для обратной совместимости с кодом, который вызывает
        llm.complete_structured() без await (например, в потоке через asyncio.to_thread).
        Вызывает primary напрямую без fallback (fallback требует async context).
        """
        method = getattr(self.primary_llm, "complete_structured", None)
        if method is None:
            raise AttributeError("primary_llm has no method 'complete_structured'")
        return method(*args, **kwargs)

    async def acomplete_structured(self, *args, **kwargs):
        return await self._complete_structured_async(*args, **kwargs)


def _label_llm(llm: Any) -> str:
    """Возвращает короткую метку LLM для логов/уведомлений."""
    if hasattr(llm, "model"):
        return f"{getattr(llm, 'provider', '?')}/{getattr(llm, 'model', '?')}"
    return type(llm).__name__




class MultiFallbackAIClient:
    """Многоуровневый fallback: Primary → Fallback1 → список OpenRouter free-моделей.

    Стратегия (см. AI_FALLBACK_STRATEGY.md, обновлено 2026-09-01):
      1. Попытка primary (например, DeepSeek)
      2. При ошибке — попытка fallback1 (например, NVIDIA Nemotron через OpenRouter)
      3. При ошибке — перебор списка бесплатных моделей OpenRouter
      4. Платные модели строго запрещены (используются только free с пометкой ":free" в названии)

    Уведомления:
      - ⚠️ При каждом переключении (primary → fallback1, fallback1 → openrouter free)
      - ❌ Если все модели недоступны
    """

    def __init__(
        self,
        primary: "AIClient | FallbackAIClient",
        fallback_chain: list,  # List of AIClient
        notify_func: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.primary = primary
        self.fallback_chain = fallback_chain
        self.notify_func = notify_func
        # Собираем цепочку: primary, затем все fallback
        self._chain = [primary] + list(fallback_chain)

    def _label(self, client) -> str:
        return _label_llm(client)

    async def _notify(self, message: str) -> None:
        if self.notify_func is None:
            return
        try:
            await self.notify_func(message)
        except Exception as exc:
            logger.warning("notify_func failed: %s", exc)

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        task: str | None = None,
        timeout: float | None = None,
        extra_body: dict | None = None,
    ) -> "AIResponse":
        """Пройти по цепочке клиентов, вернуть первый успешный ответ."""
        import time
        chain_start = time.monotonic()
        last_exc: Exception | None = None
        last_success_label: str = ""
        logger.info(
            "🔄 MultiFallbackAIClient: запуск цепочки из %d моделей (task=%s)",
            len(self._chain), task or "unknown",
        )
        for idx, client in enumerate(self._chain):
            label = self._label(client)
            step_start = time.monotonic()
            try:
                logger.info(
                    "  [%d/%d] Пробую модель: %s (endpoint=%s, proxy=%s, model=%s)",
                    idx + 1, len(self._chain), label,
                    getattr(client, "endpoint", "?"),
                    "yes" if getattr(client, "proxy", None) else "no",
                    getattr(client, "model", "?"),
                )
                resp = await client.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    task=task,
                    timeout=timeout,
                    extra_body=extra_body,
                )
                elapsed = time.monotonic() - step_start
                total_elapsed = time.monotonic() - chain_start
                logger.info(
                    "  [%d/%d] ✅ Модель %s ответила за %.1fс (всего %.1fс): %s",
                    idx + 1, len(self._chain), label, elapsed, total_elapsed,
                    (resp.content[:50] if resp.content else "(empty)"),
                )
                if idx > 0:
                    await self._notify(
                        f"🔄 Переключение на резервную модель ({idx + 1}/{len(self._chain)}): {label}\n"
                        f"(предыдущая: {last_success_label or 'primary'})"
                    )
                return resp
            except Exception as exc:
                elapsed = time.monotonic() - step_start
                logger.warning(
                    "  [%d/%d] ❌ Модель %s недоступна за %.1fс: %s: %s",
                    idx + 1, len(self._chain), label, elapsed,
                    type(exc).__name__, exc,
                )
                last_exc = exc
                if idx == 0:
                    await self._notify(
                        f"⚠️ Основная модель ({label}) недоступна: {type(exc).__name__}: {exc}\n"
                        f"🔄 Переключение на резервную цепочку ({len(self.fallback_chain)} моделей)"
                    )
                else:
                    await self._notify(
                        f"⚠️ Модель {idx + 1}/{len(self._chain)} ({label}) недоступна: {type(exc).__name__}: {exc}"
                    )
                last_success_label = label
        # Все модели в цепочке упали
        total_elapsed = time.monotonic() - chain_start
        chain_labels = ", ".join(self._label(c) for c in self._chain)
        # Получаем прокси из последнего клиента цепочки для логирования
        last_client_proxy = getattr(self._chain[-1], "proxy", None) if self._chain else None
        logger.error(
            "❌ ВСЕ %d МОДЕЛЕЙ В ЦЕПОЧКЕ НЕДОСТУПНЫ (%.1fс): %s | ПРОКСИ: %s | ПОСЛЕДНЯЯ ОШИБКА: %s: %s",
            len(self._chain), total_elapsed, chain_labels,
            "да" if last_client_proxy else ("нет" if last_client_proxy is not None else "не задан"),
            type(last_exc).__name__ if last_exc else "—", last_exc if last_exc else "—",
        )
        await self._notify(
            f"❌ Все модели в цепочке недоступны ({len(self._chain)} шт.).\n"
            f"Цепочка: {chain_labels}\n"
            f"Последняя ошибка: {type(last_exc).__name__}: {last_exc}"
        )
        if last_exc:
            raise last_exc
        raise AIConnectionError("All models in fallback chain are unavailable")

    async def acomplete(self, *args, **kwargs):
        return await self.complete(*args, **kwargs)


# ── Список бесплатных моделей OpenRouter (для многоуровневого fallback) ──
# Строго free-модели (название заканчивается на ":free"). Платные запрещены.
# Порядок от лучших к худшим моделей для fallback цепочки:
OPENROUTER_FREE_MODELS: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",        # Top tier - best free model
    "meta-llama/llama-3.3-70b-instruct:free",       # Strong instruction model
    "qwen/qwen-2.5-72b-instruct:free",              # Large Qwen model
    "google/gemma-2-9b-it:free",                      # Good Google model
    "mistralai/mistral-7b-instruct:free",           # Reliable Mistral model
    "openai/gpt-oss-20b:free",                        # Open source GPT
    "nousresearch/hermes-3-llama-3.1-405b:free",    # Hermes instruction model
    "thinkingmachines/inkling:free",                  # Specialized model
]


def _is_free_model(model_name: str) -> bool:
    """Проверяет, что модель — бесплатная (название заканчивается на ':free')."""
    return model_name.strip().endswith(":free")


def create_multi_fallback_ai_client(
    primary_endpoint: str,
    primary_api_key: str,
    primary_model: str,
    fallback1_endpoint: str,
    fallback1_api_key: str,
    fallback1_model: str,
    openrouter_api_key: str,
    openrouter_endpoint: str = "https://openrouter.ai/api/v1",
    openrouter_models: list[str] | None = None,
    user_id: int | None = None,
    username: str = "",
    bot_name: str = "bot",
    notify_func: Callable[[str], Awaitable[None]] | None = None,
    default_timeout: float = 60.0,
    proxy: str | None = None,
    primary_proxy: str | None = None,
    openrouter_proxy: str | None = None,
) -> "MultiFallbackAIClient | None":
    """Создаёт MultiFallbackAIClient: primary → fallback1 → список OpenRouter free-моделей.

    Используется стратегия:
      1. Primary (например, DeepSeek)
      2. Fallback1 (например, NVIDIA Nemotron через OpenRouter)
      3. Перебор OpenRouter free-моделей (только бесплатные!)

    Параметры:
      - primary_*: параметры основной модели
      - fallback1_*: параметры первой резервной модели
      - openrouter_api_key: ключ OpenRouter (общий для всех free-моделей)
      - openrouter_models: список моделей для перебора (по умолчанию OPENROUTER_FREE_MODELS)
      - notify_func: async-функция для уведомлений (например, _notify_admin)
    """
    if not primary_api_key or not openrouter_api_key:
        return None

    # Proxy: primary_proxy для primary, openrouter_proxy для OpenRouter fallback
    # Если указан общий proxy, используем его
    eff_primary_proxy = primary_proxy or proxy
    eff_openrouter_proxy = openrouter_proxy or proxy

    # Primary клиент
    from urllib.parse import urlparse
    primary_provider = "openrouter" if "openrouter" in primary_endpoint.lower() else urlparse(primary_endpoint).netloc or "openai"
    primary_client = AIClient(
        endpoint=primary_endpoint.rstrip("/"),
        api_key=primary_api_key,
        model=primary_model,
        provider=primary_provider,
        default_timeout=default_timeout,
        user_id=user_id or 0,
        username=username,
        bot_name=bot_name,
        proxy=eff_primary_proxy,
    )

    # Fallback1 клиент
    fb1_provider = "openrouter" if "openrouter" in fallback1_endpoint.lower() else urlparse(fallback1_endpoint).netloc or "openai"
    fallback1_client = AIClient(
        endpoint=fallback1_endpoint.rstrip("/"),
        api_key=fallback1_api_key,
        model=fallback1_model,
        provider=fb1_provider,
        default_timeout=default_timeout,
        user_id=user_id or 0,
        username=username,
        bot_name=bot_name,
        proxy=eff_openrouter_proxy,
    )

    # Цепочка OpenRouter free-моделей (строго бесплатных!)
    models_to_use = openrouter_models or OPENROUTER_FREE_MODELS
    free_models = [m for m in models_to_use if _is_free_model(m)]
    if not free_models:
        logger.warning("No free models provided for OpenRouter fallback chain")
        return None

    fallback_chain = [fallback1_client]
    for model_name in free_models:
        free_client = AIClient(
            endpoint=openrouter_endpoint.rstrip("/"),
            api_key=openrouter_api_key,
            model=model_name,
            provider="openrouter",
            default_timeout=default_timeout,
            user_id=user_id or 0,
            username=username,
            bot_name=bot_name,
            proxy=eff_openrouter_proxy,
        )
        fallback_chain.append(free_client)

    return MultiFallbackAIClient(
        primary=primary_client,
        fallback_chain=fallback_chain,
        notify_func=notify_func,
    )


__all__ = [
    "AIClient",
    "MockAIClient",
    "FallbackAIClient",
    "LLMFallbackWrapper",
    "create_fallback_ai_client",
    "AIResponse",
    "parse_structured_response",
    "strip_json_markdown",
    # ── Учёт обращений к нейросети (08.2026) ────────────────────────
    "UsageRecord",
    "UsageTracker",
    "estimate_cost",
    "format_usage_report",
    "usage_period_from_args",
    # ── Многоуровневый fallback (2026-09-01) ────────────────────────
    "MultiFallbackAIClient",
    "OPENROUTER_FREE_MODELS",
    "_is_free_model",
    "create_multi_fallback_ai_client",
]
