from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from bot.config import Settings
from bot.models.ai import NargesReply
from bot.models.state import NargesSelfStateCandidate
from bot.storage.database import Database
from bot.storage.orm import AiProviderKeyStatusORM


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroqResult:
    reply: NargesReply
    raw_text: str
    usage: dict[str, int | None]
    provider: str = "unknown"
    model: str = "unknown"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    model: str
    api_keys: tuple[str, ...]
    base_url: str | None = None
    enabled: bool = True
    temperature: float = 0.7
    max_completion_tokens: int = 512
    timeout_seconds: float = 45
    response_format: str = "json_object"
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    prompt_cache: bool = False


class ProviderRequestError(RuntimeError):
    def __init__(self, provider: str, message: str, retryable: bool = True) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


class GroqChatClient:
    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.database = database
        self.providers = self._load_providers(settings)
        proxy = settings.groq_proxy
        self.http_client = httpx.Client(proxy=proxy, timeout=45) if proxy else httpx.Client(timeout=45)

    def complete(self, messages: list[dict[str, str]]) -> GroqResult:
        raw_text, usage, provider, model = self._complete_json(messages, NargesReply.model_json_schema(), "chat")
        payload = self._loads_json(raw_text)
        return GroqResult(
            reply=NargesReply.validate_provider_payload(payload),
            raw_text=raw_text,
            usage=usage,
            provider=provider,
            model=model,
        )

    def complete_narges_state(self, messages: list[dict[str, str]]) -> tuple[NargesSelfStateCandidate, dict[str, int | None]]:
        raw_text, usage, _provider, _model = self._complete_json(
            messages,
            NargesSelfStateCandidate.model_json_schema(),
            "narges_state",
            max_completion_tokens=500,
            temperature=0.8,
        )
        payload = self._loads_json(raw_text)
        return NargesSelfStateCandidate.model_validate(payload), usage

    def _complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[str, dict[str, int | None], str, str]:
        last_error: Exception | None = None
        for provider in self.providers:
            if not provider.enabled:
                continue
            keys = self._resolve_keys(provider)
            if not keys:
                continue
            for key_index, api_key in enumerate(keys):
                if self._key_is_temporarily_disabled(provider.name, key_index):
                    continue
                try:
                    return self._request_provider(
                        provider,
                        api_key,
                        messages,
                        schema,
                        purpose,
                        key_index=key_index,
                        max_completion_tokens=max_completion_tokens,
                        temperature=temperature,
                    )
                except ProviderRequestError as exc:
                    last_error = exc
                    self._record_key_failure(provider.name, key_index, exc)
                    if not exc.retryable:
                        break
                    logger.warning("ai_provider_failed provider=%s retryable=%s error=%s", provider.name, exc.retryable, exc)
        if last_error:
            raise last_error
        raise RuntimeError("No configured AI provider has an available API key.")

    def _request_provider(
        self,
        provider: ProviderConfig,
        api_key: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        *,
        key_index: int,
        max_completion_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, int | None], str, str]:
        if provider.kind == "gemini_interactions":
            url, headers, payload = self._gemini_request(provider, api_key, messages, schema, max_completion_tokens, temperature)
        else:
            url, headers, payload = self._openai_compatible_request(
                provider,
                api_key,
                messages,
                schema,
                max_completion_tokens,
                temperature,
            )
        self._log_ai_request(purpose, provider, key_index, payload)
        response = self._post_with_fallbacks(provider, url, headers, payload, purpose)
        if response.status_code >= 400:
            retryable = response.status_code in {401, 403, 408, 409, 429} or response.status_code >= 500
            retry_after = self._retry_after(response)
            self._log_ai_error(purpose, provider, payload, RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}"))
            message = f"HTTP {response.status_code}"
            if retry_after:
                message = f"{message}; retry_after_seconds={retry_after}"
            raise ProviderRequestError(provider.name, message, retryable=retryable)
        data = response.json()
        raw_text = self._extract_text(provider, data)
        usage = self._extract_usage(data)
        self._record_key_success(provider.name, key_index)
        self._log_ai_response(purpose, provider, raw_text, usage)
        return raw_text, usage, provider.name, self._extract_model(data, provider.model)

    def _openai_compatible_request(
        self,
        provider: ProviderConfig,
        api_key: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        max_completion_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        base_url = (provider.base_url or "").rstrip("/")
        if not base_url:
            raise ProviderRequestError(provider.name, "base_url is required", retryable=False)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(provider.extra_headers or {}),
        }
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": provider.temperature if temperature is None else temperature,
            "max_completion_tokens": max_completion_tokens or provider.max_completion_tokens,
            "stream": False,
            **(provider.extra_body or {}),
        }
        if provider.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "narges_structured_response",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif provider.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return f"{base_url}/chat/completions", headers, payload

    def _post_with_fallbacks(
        self,
        provider: ProviderConfig,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        purpose: str,
    ) -> httpx.Response:
        variants = [payload]
        if "response_format" in payload:
            without_format = dict(payload)
            without_format.pop("response_format", None)
            variants.append(without_format)
        if "max_completion_tokens" in payload:
            max_tokens_variant = dict(payload)
            max_tokens_variant["max_tokens"] = max_tokens_variant.pop("max_completion_tokens")
            variants.append(max_tokens_variant)
            if "response_format" in max_tokens_variant:
                max_tokens_without_format = dict(max_tokens_variant)
                max_tokens_without_format.pop("response_format", None)
                variants.append(max_tokens_without_format)

        last_response: httpx.Response | None = None
        for index, variant in enumerate(variants):
            try:
                response = self.http_client.post(url, headers=headers, json=variant, timeout=provider.timeout_seconds)
            except httpx.HTTPError as exc:
                self._log_ai_error(purpose, provider, variant, exc)
                raise ProviderRequestError(provider.name, exc.__class__.__name__, retryable=True) from exc
            last_response = response
            if response.status_code != 400:
                return response
            if index == 0:
                self._log_ai_error(
                    purpose,
                    provider,
                    variant,
                    RuntimeError(f"HTTP 400, retrying compatible payload: {response.text[:500]}"),
                )
        return last_response  # type: ignore[return-value]

    def _gemini_request(
        self,
        provider: ProviderConfig,
        api_key: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        max_completion_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        system_text = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        input_text = "\n\n".join(item["content"] for item in messages if item["role"] != "system")
        payload: dict[str, Any] = {
            "model": provider.model,
            "system_instruction": system_text,
            "input": input_text,
            "generation_config": {
                "temperature": provider.temperature if temperature is None else temperature,
                "max_output_tokens": max_completion_tokens or provider.max_completion_tokens,
            },
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
            **(provider.extra_body or {}),
        }
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json", **(provider.extra_headers or {})}
        return provider.base_url or "https://generativelanguage.googleapis.com/v1beta/interactions", headers, payload

    def _extract_text(self, provider: ProviderConfig, data: dict[str, Any]) -> str:
        if provider.kind == "gemini_interactions":
            if isinstance(data.get("output_text"), str):
                return data["output_text"]
            if isinstance(data.get("text"), str):
                return data["text"]
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [part.get("text", "") for part in content if isinstance(part, dict)]
                return "\n".join(text for text in texts if text)
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates:
            parts = ((candidates[0].get("content") or {}).get("parts") or [])
            texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
            if texts:
                return "\n".join(texts)
        return "{}"

    def _extract_model(self, data: dict[str, Any], fallback: str) -> str:
        value = data.get("model")
        return str(value) if value else fallback

    def _extract_usage(self, data: dict[str, Any]) -> dict[str, int | None]:
        usage = data.get("usage") or data.get("usageMetadata") or {}
        return {
            "prompt_tokens": self._usage_value(usage, "prompt_tokens", "promptTokenCount"),
            "completion_tokens": self._usage_value(usage, "completion_tokens", "candidatesTokenCount"),
            "total_tokens": self._usage_value(usage, "total_tokens", "totalTokenCount"),
        }

    def _usage_value(self, usage: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _loads_json(self, raw_text: str) -> dict[str, Any]:
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    def _load_providers(self, settings: Settings) -> list[ProviderConfig]:
        path = Path(settings.ai_providers_config)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            providers = raw.get("providers", raw if isinstance(raw, list) else [])
            return [self._provider_from_dict(item) for item in providers if isinstance(item, dict)]
        return [
            ProviderConfig(
                name="groq",
                kind="openai_compatible",
                base_url="https://api.groq.com/openai/v1",
                api_keys=("env:GROQ_API_KEY",),
                model=settings.groq_model,
                temperature=settings.groq_temperature,
                max_completion_tokens=settings.groq_max_completion_tokens,
                response_format="json_object",
            )
        ]

    def _provider_from_dict(self, item: dict[str, Any]) -> ProviderConfig:
        return ProviderConfig(
            name=str(item["name"]).strip(),
            kind=str(item.get("type") or item.get("kind") or "openai_compatible").strip(),
            base_url=str(item.get("base_url") or "").strip() or None,
            api_keys=tuple(str(value).strip() for value in item.get("api_keys", []) if str(value).strip()),
            model=str(item["model"]).strip(),
            enabled=bool(item.get("enabled", True)),
            temperature=float(item.get("temperature", self.settings.groq_temperature)),
            max_completion_tokens=int(item.get("max_completion_tokens", self.settings.groq_max_completion_tokens)),
            timeout_seconds=float(item.get("timeout_seconds", 45)),
            response_format=str(item.get("response_format", "json_object")),
            extra_headers=dict(item.get("extra_headers") or {}),
            extra_body=dict(item.get("extra_body") or {}),
            prompt_cache=bool(item.get("prompt_cache", False)),
        )

    def _resolve_keys(self, provider: ProviderConfig) -> list[str]:
        keys: list[str] = []
        for raw in provider.api_keys:
            if raw.startswith("env:"):
                value = os.getenv(raw[4:], "").strip()
                if value:
                    keys.append(value)
            elif raw.startswith("$"):
                value = os.getenv(raw[1:], "").strip()
                if value:
                    keys.append(value)
            elif raw:
                keys.append(raw)
        return keys

    def _log_ai_request(self, purpose: str, provider: ProviderConfig, key_index: int, payload: dict[str, Any]) -> None:
        safe_payload = dict(payload)
        line = "AI_REQUEST " + json.dumps(
            {"purpose": purpose, "provider": provider.name, "model": provider.model, "key_index": key_index, "payload": safe_payload},
            ensure_ascii=False,
            default=str,
        )
        logger.info(line)

    def _log_ai_response(self, purpose: str, provider: ProviderConfig, raw_text: str, usage: dict[str, int | None]) -> None:
        line = "AI_RESPONSE " + json.dumps(
            {"purpose": purpose, "provider": provider.name, "raw_text": raw_text, "usage": usage},
            ensure_ascii=False,
            default=str,
        )
        logger.info(line)

    def _log_ai_error(self, purpose: str, provider: ProviderConfig, payload: dict[str, Any], exc: Exception) -> None:
        line = "AI_ERROR " + json.dumps(
            {
                "purpose": purpose,
                "provider": provider.name,
                "model": provider.model,
                "payload": payload,
                "error": exc.__class__.__name__,
                "message": str(exc),
            },
            ensure_ascii=False,
            default=str,
        )
        logger.exception(line)

    def _retry_after(self, response: httpx.Response) -> int | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return 3600 if response.status_code == 429 else None
        try:
            return max(60, min(int(float(raw)), 24 * 3600))
        except ValueError:
            return 3600

    def _key_is_temporarily_disabled(self, provider: str, key_index: int) -> bool:
        if self.database is None:
            return False
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(AiProviderKeyStatusORM, {"provider": provider, "key_index": key_index})
            if row is None or row.disabled_until is None:
                return False
            disabled_until = row.disabled_until if isinstance(row.disabled_until, datetime) else datetime.fromisoformat(row.disabled_until)
            if disabled_until.tzinfo is None:
                disabled_until = disabled_until.replace(tzinfo=UTC)
            return disabled_until > now

    def _record_key_success(self, provider: str, key_index: int) -> None:
        if self.database is None:
            return
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(AiProviderKeyStatusORM, {"provider": provider, "key_index": key_index})
            if row is None:
                row = AiProviderKeyStatusORM(provider=provider, key_index=key_index, status="ok", updated_at=now)
                session.add(row)
            row.status = "ok"
            row.error_count = 0
            row.last_error = None
            row.disabled_until = None
            row.last_success_at = now
            row.updated_at = now

    def _record_key_failure(self, provider: str, key_index: int, exc: ProviderRequestError) -> None:
        if self.database is None:
            return
        now = datetime.now(UTC)
        retry_after = self._retry_after_from_message(str(exc))
        disabled_until = now + timedelta(seconds=retry_after) if retry_after else None
        status = "limited" if retry_after else "failed"
        with self.database.orm.session() as session:
            row = session.get(AiProviderKeyStatusORM, {"provider": provider, "key_index": key_index})
            if row is None:
                row = AiProviderKeyStatusORM(provider=provider, key_index=key_index, status=status, updated_at=now)
                session.add(row)
            row.status = status
            row.error_count = int(row.error_count or 0) + 1
            row.last_error = str(exc)[:500]
            row.disabled_until = disabled_until
            row.updated_at = now

    def _retry_after_from_message(self, message: str) -> int | None:
        if "HTTP 429" in message and "retry_after_seconds=" not in message:
            return 3600
        marker = "retry_after_seconds="
        if marker not in message:
            return None
        raw = message.split(marker, 1)[1].split(";", 1)[0].strip()
        try:
            return max(60, min(int(float(raw)), 24 * 3600))
        except ValueError:
            return 3600
