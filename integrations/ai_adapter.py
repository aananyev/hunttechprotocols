"""
AI adapter for hunttech-bot-common.

Replaces the inline call_ai() and _test_ai_connection() with calls to
the common AIClient, preserving exact error message format, timeout behavior,
and OpenRouter headers.
"""
import logging

import httpx
from hunttech_bot_common.ai import AIClient

logger = logging.getLogger("bot")


async def call_ai_with_config(ai_config: dict, system_prompt: str, user_text: str) -> str:
    """Core AI call using the common AIClient.

    Args:
        ai_config: User's AI configuration dict from get_ai_config().
        system_prompt: System-level instructions.
        user_text: User message.

    Returns:
        str — AI response text or ❌ error message (never raises).
    """
    if not ai_config:
        return "❌ AI не настроен. Используйте `/setup_ai`"

    endpoint = ai_config.get("endpoint", "").rstrip("/")
    api_key = ai_config.get("api_key", "")
    model = ai_config.get("model", "gpt-4o")

    if not endpoint or not api_key:
        return "❌ AI настроен не полностью. Проверьте endpoint и API key через `/setup_ai`"

    extra_headers = {}
    if "openrouter" in endpoint.lower():
        extra_headers["HTTP-Referer"] = "https://t.me/hunttech_protocols_bot"
        extra_headers["X-Title"] = "HunttechProtocolsBot"

    provider = "openai"
    if "openrouter" in endpoint.lower():
        provider = "openrouter"
    elif "deepseek" in endpoint.lower():
        provider = "deepseek"

    try:
        client = AIClient(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            provider=provider,
            timeout=120.0,
            max_retries=1,
            retry_delay=2.0,
            extra_headers=extra_headers,
        )

        response = await client.complete(
            system_prompt=system_prompt,
            user_prompt=user_text,
        )
        return response.content

    except httpx.TimeoutException:
        return "❌ Таймаут: нейросеть не ответила за 120 секунд"
    except Exception as e:
        return f"❌ Ошибка: {e}"


async def test_ai_connection(endpoint: str, api_key: str, model: str) -> str:
    """Test AI connection using the common AIClient.

    Args:
        endpoint: API endpoint URL.
        api_key: API key.
        model: Model name.

    Returns:
        str — success message with model reply or ❌ error.
    """
    endpoint = endpoint.rstrip("/")
    extra_headers = {}
    if "openrouter" in endpoint.lower():
        extra_headers["HTTP-Referer"] = "https://t.me/hunttech_protocols_bot"
        extra_headers["X-Title"] = "HunttechProtocolsBot"

    provider = "openai"
    if "openrouter" in endpoint.lower():
        provider = "openrouter"
    elif "deepseek" in endpoint.lower():
        provider = "deepseek"

    try:
        client = AIClient(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            provider=provider,
            timeout=15.0,
            max_retries=0,
            extra_headers=extra_headers,
        )

        response = await client.complete(
            system_prompt="Ответь одним словом: привет",
            user_prompt="Ответь одним словом: привет",
            max_tokens=10,
        )

        return f"✅ Подключение успешно!\nОтвет модели: «{response.content.strip()}»"

    except httpx.TimeoutException:
        return "❌ Таймаут: сервер не ответил за 15 секунд. Проверьте endpoint."
    except httpx.ConnectError:
        return "❌ Не удалось подключиться к серверу. Проверьте endpoint."
    except Exception as e:
        if hasattr(e, 'status_code'):
            status = e.status_code
            if status == 401:
                return "❌ Ошибка авторизации (401). Проверьте API-ключ."
            elif status == 404:
                return "❌ Модель не найдена (404). Проверьте название модели."
            return f"❌ Ошибка API ({status}): {str(e)[:300]}"
        return f"❌ Ошибка: {e}"
