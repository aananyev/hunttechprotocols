"""AI Key Vault client for the protocols Telegram bot."""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from jwcrypto import jwk, jwe, jwt

logger = logging.getLogger(__name__)

PROVIDER_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/v1",
    "anymodel": "https://anymodel.org/v1",
}


class VaultAIError(RuntimeError):
    """Safe error without provider response bodies or credential material."""


@dataclass
class VaultAIResponse:
    content: str
    provider: str
    model: str
    duration_ms: float
    usage: dict[str, int]


def _read_secret(value: str) -> str:
    if value.startswith("file:"):
        return Path(value[5:]).read_text(encoding="utf-8").strip()
    return value.strip()


def _parse_chain() -> list[tuple[str, str, str]]:
    primary_provider = os.getenv("VAULT_PRIMARY_PROVIDER", "deepseek").strip().lower()
    primary_model = os.getenv("VAULT_PRIMARY_MODEL", "deepseek-chat").strip()
    chain = [(primary_provider, primary_model)]
    for item in os.getenv("VAULT_FALLBACK_MODELS", "anymodel=am/nemotron-3-ultra-550b-a55b").split(";"):
        provider, separator, model = item.strip().partition("=")
        if separator and provider and model and (provider, model) not in chain:
            chain.append((provider.strip().lower(), model.strip()))
    result = []
    for provider, model in chain:
        endpoint = PROVIDER_ENDPOINTS.get(provider)
        if not endpoint or not model:
            raise VaultAIError("Vault model chain contains an unsupported provider or empty model")
        result.append((provider, model, endpoint))
    return result


class VaultAIClient:
    """Leases a short-lived key per model attempt and never uses legacy keys."""

    def __init__(self, user_id: int, bot_name: str = "hunttech-protocols-bot") -> None:
        self.user_id = int(user_id)
        self.bot_name = bot_name
        self.base_url = os.getenv("VAULT_URL", "").rstrip("/")
        self.client_id = os.getenv("VAULT_CLIENT_ID", "protocols-bot-prod")
        self.client_token = _read_secret(os.getenv("VAULT_CLIENT_TOKEN", ""))
        self.jwt_issuer = os.getenv("VAULT_JWT_ISSUER", "")
        self.signing_kid = os.getenv("VAULT_SIGNING_KID", "")
        self.encryption_kid = os.getenv("VAULT_ENCRYPTION_KID", "")
        signing_pem = _read_secret(os.getenv("VAULT_SIGNING_PRIVATE_KEY", ""))
        encryption_pem = _read_secret(os.getenv("VAULT_ENCRYPTION_PRIVATE_KEY", ""))
        if not all((self.base_url, self.client_token, self.jwt_issuer,
                    self.signing_kid, self.encryption_kid, signing_pem, encryption_pem)):
            raise VaultAIError("Vault configuration is incomplete")
        parsed = urlsplit(self.base_url)
        test_http = (
            os.getenv("VAULT_ALLOW_INSECURE_TEST", "false").lower() == "true"
            and parsed.scheme == "http" and parsed.hostname == "vault"
            and self.client_id.endswith("-test")
        )
        if parsed.scheme != "https" and not test_http:
            raise VaultAIError("Vault URL must use HTTPS outside the isolated test profile")

        self.signing_key = jwk.JWK.from_pem(signing_pem.encode("utf-8"))
        self.encryption_key = jwk.JWK.from_pem(encryption_pem.encode("utf-8"))
        self.cert = os.getenv("VAULT_MTLS_CERT") or None
        self.key = os.getenv("VAULT_MTLS_KEY") or None
        self.require_mtls = os.getenv("VAULT_REQUIRE_MTLS", "true").lower() in {"1", "true", "yes"}
        if self.require_mtls and not (self.cert and self.key):
            raise VaultAIError("Vault mutual TLS is required but not configured")
        self.ca_bundle = os.getenv("VAULT_CA_BUNDLE") or None
        self.ssl_context = ssl.create_default_context(cafile=self.ca_bundle)
        if self.require_mtls:
            self.ssl_context.load_cert_chain(self.cert, self.key)
        self.model_chain = _parse_chain()

    def _user_assertion(self) -> str:
        now = int(time.time())
        signed = jwt.JWT(
            header={"alg": "RS256", "kid": self.signing_kid, "typ": "JWT"},
            claims={"iss": self.jwt_issuer, "sub": str(self.user_id), "aud": "ai-key-vault",
                    "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())},
        )
        signed.make_signed_token(self.signing_key)
        return signed.serialize()

    async def _lease_key(self, provider: str, model: str, correlation_id: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.client_token}",
            "X-Client-Id": self.client_id,
            "X-User-Assertion": self._user_assertion(),
            "X-Correlation-Id": correlation_id,
            "Idempotency-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        payload = {"provider": provider, "model": model,
                   "capability": "chat.completions", "purpose": "protocol-summary"}
        timeout = httpx.Timeout(12.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, verify=self.ssl_context,
                                     trust_env=False) as client:
            response = await client.post(f"{self.base_url}/api/v1/credentials/lease",
                                         headers=headers, json=payload)
        if response.status_code != 200:
            raise VaultAIError(f"Vault denied key lease (HTTP {response.status_code})")
        body = response.json()
        encrypted = body.get("encryptedCredential") or {}
        if (body.get("provider") != provider or encrypted.get("format") != "compact-jwe"
                or encrypted.get("kid") != self.encryption_kid):
            raise VaultAIError("Vault returned an unexpected encrypted credential")
        compact = encrypted.get("value")
        if not isinstance(compact, str) or compact.count(".") != 4:
            raise VaultAIError("Vault returned an invalid encrypted credential")
        envelope = jwe.JWE()
        envelope.deserialize(compact, key=self.encryption_key)
        protected = envelope.jose_header or {}
        if (protected.get("kid") != self.encryption_kid
                or protected.get("alg") != "RSA-OAEP-256"
                or protected.get("enc") != "A256GCM"):
            raise VaultAIError("Vault JWE header does not match the configured key profile")
        credential = json.loads(envelope.payload.decode("utf-8"))
        now = int(time.time())
        if (credential.get("aud") != self.client_id
                or credential.get("client_id") != self.client_id
                or credential.get("lease_id") != body.get("leaseId")
                or credential.get("provider") != provider
                or not isinstance(credential.get("exp"), int)
                or credential["exp"] <= now or credential["exp"] > now + 300):
            raise VaultAIError("Vault credential binding or expiry validation failed")
        api_key = credential.get("apiKey")
        if not isinstance(api_key, str) or not api_key:
            raise VaultAIError("Vault returned an empty API credential")
        return api_key

    async def complete(self, system_prompt: str, user_prompt: str, task: str = "unknown",
                       temperature: float | None = None, max_tokens: int | None = None,
                       **_: Any) -> VaultAIResponse:
        correlation_id = str(uuid.uuid4())
        started = time.monotonic()
        failures: list[str] = []
        for attempt, (provider, model, endpoint) in enumerate(self.model_chain, start=1):
            attempt_started = time.monotonic()
            status_code = 0
            try:
                api_key = await self._lease_key(provider, model, correlation_id)
                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                request = {"model": model, "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]}
                if temperature is not None:
                    request["temperature"] = temperature
                if max_tokens is not None:
                    request["max_tokens"] = max_tokens
                async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                    response = await client.post(f"{endpoint}/chat/completions",
                                                 headers=headers, json=request)
                status_code = response.status_code
                if status_code != 200:
                    failures.append(f"{provider}:{status_code}")
                else:
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage") or {}
                    logger.info(
                        "ai_model_attempt correlation_id=%s bot=%s user_id=%s task=%s attempt=%d provider=%s model=%s outcome=success http_status=%d duration_ms=%d",
                        correlation_id, self.bot_name, self.user_id, task, attempt, provider, model,
                        status_code, int((time.monotonic() - attempt_started) * 1000),
                    )
                    return VaultAIResponse(
                        content=str(content or ""), provider=provider, model=model,
                        duration_ms=(time.monotonic() - started) * 1000,
                        usage={key: int(usage.get(key) or 0)
                               for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    )
            except Exception as exc:
                failures.append(type(exc).__name__)
            logger.info(
                "ai_model_attempt correlation_id=%s bot=%s user_id=%s task=%s attempt=%d provider=%s model=%s outcome=error http_status=%d duration_ms=%d",
                correlation_id, self.bot_name, self.user_id, task, attempt, provider, model,
                status_code, int((time.monotonic() - attempt_started) * 1000),
            )
        logger.error("ai_model_chain_failed correlation_id=%s task=%s failures=%s",
                     correlation_id, task, ",".join(failures))
        raise VaultAIError("All authorized AI model attempts failed")
