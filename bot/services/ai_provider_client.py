from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

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

CHAT_REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 1800},
                    "delay_seconds": {"type": "number", "minimum": 0, "maximum": 2},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["messages"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ProviderResult:
    reply: NargesReply
    raw_text: str
    usage: dict[str, int | None]
    provider: str = "unknown"
    model: str = "unknown"
    provider_response_id: str | None = None


@dataclass(frozen=True)
class ImageSelectionResult:
    image_id: str | None
    caption: str | None
    usage: dict[str, int | None]
    provider: str = "unknown"
    model: str = "unknown"


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    kind: str
    model: str
    api_keys: tuple[str, ...]
    status_name: str
    priority: int | None = None
    base_url: str | None = None
    enabled: bool = True
    temperature: float = 0.7
    max_completion_tokens: int = 320
    timeout_seconds: float = 35
    response_format: str = "json_object"
    token_parameter: str = "max_completion_tokens"
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    prompt_cache: bool = False
    health_check: bool = False
    experimental: bool = False
    use_proxy: bool = True


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        key_scoped: bool = False,
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.key_scoped = key_scoped




class AIProviderClient:
    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.database = database
        self._key_cursors: dict[str, int] = {}
        self._key_cursor_lock = threading.Lock()
        self._providers_path = Path(settings.ai_providers_config)
        self._providers_fingerprint: str | None = None
        self.providers = self._load_providers(settings)
        self._health_cache: dict[str, datetime] = {}
        self._provider_disabled_until: dict[str, datetime] = {}
        self.proxy_url = settings.groq_proxy
        self.http_client = httpx.Client(proxy=self.proxy_url, timeout=35) if self.proxy_url else httpx.Client(timeout=35)
        self.direct_http_client = httpx.Client(timeout=35)
        self._user_provider_overrides: dict[int, str] = {}

    def close(self) -> None:
        self.http_client.close()
        self.direct_http_client.close()

    def complete(self, messages: list[dict[str, str]], forced_provider: str | None = None) -> ProviderResult:
        raw_text, usage, provider, model, response_id = self._complete_raw(
            messages,
            NargesReply.model_json_schema(),
            "chat",
            max_completion_tokens=None,
            temperature=None,
            forced_provider=forced_provider,
        )
        return self._decode_chat_result(raw_text, usage, provider, model, response_id)

    def complete_structured(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
        forced_provider: str | None = None,
    ) -> tuple[str, dict[str, int | None], str, str]:
        raw_text, usage, provider, model, _response_id = self._complete_raw(
            messages,
            schema,
            purpose,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
            forced_provider=forced_provider,
        )
        return raw_text, usage, provider, model

    def _complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        *,
        max_completion_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[str, dict[str, int | None], str, str]:
        return self.complete_structured(
            messages,
            schema,
            purpose,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
        )

    def set_provider_override(self, user_id: int, provider: str) -> None:
        self._user_provider_overrides[int(user_id)] = provider.strip()

    def clear_provider_override(self, user_id: int) -> None:
        self._user_provider_overrides.pop(int(user_id), None)

    def provider_override_for_user(self, user_id: int) -> str | None:
        return self._user_provider_overrides.get(int(user_id))

    def provider_choices(self) -> list[str]:
        self._reload_providers_if_changed()
        return [f"{provider.id}:{provider.model}" for provider in self.providers if provider.enabled]

    def complete_conversation_summary(self, existing_summary: str, messages: list[dict[str, str]]) -> str:
        summary, _usage, _provider, _model = self.complete_conversation_summary_with_usage(existing_summary, messages)
        return summary

    def complete_conversation_summary_with_usage(
        self,
        existing_summary: str,
        messages: list[dict[str, str]],
    ) -> tuple[str, dict[str, int | None], str, str]:
        compact_messages = [
            {
                "role": str(item.get("role") or ""),
                "text": self._compact_text(str(item.get("text") or ""), 350),
                "created_at": item.get("created_at"),
                "intent": item.get("intent"),
            }
            for item in messages[-20:]
            if item.get("text")
        ]
        payload = {
            "existing_summary": self._compact_text(existing_summary, 450),
            "new_messages": compact_messages,
        }
        schema = {
            "type": "object",
            "properties": {"summary": {"type": "string", "maxLength": 700}},
            "required": ["summary"],
            "additionalProperties": False,
        }
        raw_text, usage, provider, model = self.complete_structured(
            [
                {
                    "role": "system",
                    "content": "یک خلاصهٔ factual و فشرده برای ادامهٔ گفت‌وگو بساز. فقط اطلاعات پایدار و موضوعات حل‌نشده را نگه دار. فقط JSON بده.",
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            schema,
            "conversation_summary",
            max_completion_tokens=220,
            temperature=0.15,
        )
        parsed = self._try_loads_json(raw_text) or {}
        return self._compact_text(str(parsed.get("summary") or raw_text), 700), usage, provider, model

    def complete_narges_state(self, messages: list[dict[str, str]]) -> tuple[NargesSelfStateCandidate, dict[str, int | None]]:
        raw_text, usage, _provider, _model = self.complete_structured(
            messages,
            NargesSelfStateCandidate.model_json_schema(),
            "narges_state",
            max_completion_tokens=320,
            temperature=0.5,
        )
        payload = self._normalize_narges_state_payload(self._loads_json(raw_text))
        return NargesSelfStateCandidate.model_validate(payload), usage

    def complete_image_selection(
        self,
        *,
        original_messages: list[dict[str, str]],
        image_request: dict[str, Any],
        image_catalog: list[dict[str, Any]],
    ) -> ImageSelectionResult:
        schema = {
            "type": "object",
            "properties": {
                "image_id": {"type": ["string", "null"]},
                "caption": {"type": ["string", "null"], "maxLength": 500},
            },
            "required": ["image_id", "caption"],
            "additionalProperties": False,
        }
        user_context = ""
        for message in reversed(original_messages):
            if message.get("role") == "user":
                user_context = self._compact_text(message.get("content", ""), 1000)
                break
        catalog = []
        for item in image_catalog[:40]:
            catalog.append(
                {
                    "id": item.get("id"),
                    "description": self._compact_text(str(item.get("description") or item.get("caption") or item.get("name") or ""), 240),
                    "tags": item.get("tags") if isinstance(item.get("tags"), list) else None,
                }
            )
        payload = {
            "request": image_request,
            "catalog": catalog,
            "user_context": user_context,
        }
        raw_text, usage, provider, model = self.complete_structured(
            [
                {"role": "system", "content": "فقط در صورت تطابق روشن یک تصویر محلی انتخاب کن. شناسه نساز. فقط JSON بده."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)},
            ],
            schema,
            "image_selection",
            max_completion_tokens=120,
            temperature=0.1,
        )
        parsed = self._try_loads_json(raw_text) or {}
        image_id = parsed.get("image_id")
        caption = parsed.get("caption")
        return ImageSelectionResult(
            image_id=str(image_id).strip() if image_id else None,
            caption=self._compact_text(str(caption), 500) if caption else None,
            usage=usage,
            provider=provider,
            model=model,
        )

    def _complete_raw(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        *,
        max_completion_tokens: int | None,
        temperature: float | None,
        forced_provider: str | None,
    ) -> tuple[str, dict[str, int | None], str, str, str | None]:
        overall_started = time.perf_counter()
        self._reload_providers_if_changed()
        last_error: ProviderRequestError | None = None
        candidates = self._candidate_providers(forced_provider)
        logger.info(
            "AI_PURPOSE_START purpose=%s forced_provider=%s candidates=%s input_messages=%s",
            purpose,
            forced_provider,
            [provider.id for provider in candidates if provider.enabled],
            len(messages),
        )
        for provider in candidates:
            if not provider.enabled or self._provider_is_temporarily_disabled(provider.id):
                continue
            keys = self._resolve_keys(provider)
            if not keys:
                continue
            failed_keys = 0
            attempted_keys = 0
            failure_signatures: dict[tuple[int | None, str], int] = {}
            for key_index, api_key in self._ordered_keys(provider.id, keys):
                if self._key_is_temporarily_disabled(provider.id, key_index):
                    continue
                attempted_keys += 1
                logger.info(
                    "AI_ATTEMPT_START purpose=%s provider=%s key_index=%s attempt=%s elapsed_ms=%s",
                    purpose,
                    provider.id,
                    key_index,
                    attempted_keys,
                    int((time.perf_counter() - overall_started) * 1000),
                )
                if provider.health_check and not self._provider_is_healthy(provider, api_key):
                    error = ProviderRequestError(provider.name, "health check failed", retryable=True, key_scoped=True)
                    self._record_key_failure(provider.id, key_index, error)
                    last_error = error
                    failed_keys += 1
                    signature = (error.status_code, str(error))
                    failure_signatures[signature] = failure_signatures.get(signature, 0) + 1
                    continue
                try:
                    response = self._request_provider(
                        provider,
                        api_key,
                        messages,
                        schema,
                        purpose,
                        key_index=key_index,
                        max_completion_tokens=max_completion_tokens,
                        temperature=temperature,
                    )
                    self._advance_key_cursor(provider.id, key_index, len(keys))
                    logger.info(
                        "AI_PURPOSE_COMPLETE purpose=%s provider=%s key_index=%s attempts=%s total_latency_ms=%s total_tokens=%s",
                        purpose,
                        provider.id,
                        key_index,
                        attempted_keys,
                        int((time.perf_counter() - overall_started) * 1000),
                        response[1].get("total_tokens"),
                    )
                    return response
                except ProviderRequestError as exc:
                    last_error = exc
                    failed_keys += 1
                    signature = (exc.status_code, str(exc))
                    failure_signatures[signature] = failure_signatures.get(signature, 0) + 1
                    self._record_key_failure(provider.id, key_index, exc)
                    self._advance_key_cursor(provider.id, key_index, len(keys))
                    logger.warning(
                        "AI_KEY_FAILED purpose=%s provider=%s key_index=%s failed_keys=%s error=%s",
                        purpose,
                        provider.id,
                        key_index,
                        failed_keys,
                        str(exc)[:240],
                    )
                    continue
            repeated_failure_count = max(failure_signatures.values(), default=0)
            if repeated_failure_count >= 2:
                self._disable_provider_temporarily(provider.id, last_error or ProviderRequestError(provider.name, "multiple key failures"))
                logger.warning(
                    "AI_PROVIDER_COOLDOWN purpose=%s provider=%s failed_keys=%s total_latency_ms=%s",
                    purpose,
                    provider.id,
                    repeated_failure_count,
                    int((time.perf_counter() - overall_started) * 1000),
                )
        if last_error is not None:
            raise last_error
        if forced_provider:
            raise ProviderRequestError(forced_provider, "provider not found or no available key", retryable=False)
        raise ProviderRequestError("all", "no configured provider has an available key", retryable=True)

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
    ) -> tuple[str, dict[str, int | None], str, str, str | None]:
        if provider.kind == "gemini_interactions":
            url, headers, payload = self._gemini_request(
                provider,
                api_key,
                messages,
                schema,
                max_completion_tokens,
                temperature,
            )
        else:
            url, headers, payload = self._openai_compatible_request(
                provider,
                api_key,
                messages,
                schema,
                purpose,
                max_completion_tokens,
                temperature,
            )
        self._log_ai_request(purpose, provider, key_index, payload)
        started = datetime.now(UTC)
        try:
            response = self._client_for_provider(provider).post(
                url,
                headers=headers,
                json=payload,
                timeout=provider.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            self._log_ai_error(purpose, provider, exc, None)
            raise ProviderRequestError(provider.name, "timeout", retryable=True, key_scoped=False) from exc
        except httpx.HTTPError as exc:
            self._log_ai_error(purpose, provider, exc, None)
            raise ProviderRequestError(provider.name, exc.__class__.__name__, retryable=True, key_scoped=False) from exc

        latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        if response.status_code >= 400:
            retry_after = self._retry_after(response)
            body = self._compact_text(response.text, 500)
            error = self._classify_http_error(provider.name, response.status_code, body, retry_after)
            self._log_ai_error(purpose, provider, error, response.status_code)
            raise error
        try:
            data = response.json()
        except ValueError as exc:
            self._log_ai_error(purpose, provider, exc, response.status_code)
            raise ProviderRequestError(provider.name, "invalid JSON HTTP response", retryable=True) from exc

        raw_text = self._extract_text(provider, data)
        if not raw_text.strip():
            raise ProviderRequestError(provider.name, "empty model response", retryable=True)
        usage = self._extract_usage(data)
        model = self._extract_model(data, provider.model)
        response_id = str(data.get("id")) if data.get("id") else None
        self._record_key_success(provider.id, key_index)
        self._log_ai_response(purpose, provider, raw_text, usage, latency_ms)
        return raw_text, usage, provider.name, model, response_id

    def _classify_http_error(
        self,
        provider: str,
        status_code: int,
        body: str,
        retry_after: int | None,
    ) -> ProviderRequestError:
        if status_code == 429:
            return ProviderRequestError(
                provider,
                f"HTTP 429; retry_after_seconds={retry_after or 3600}",
                retryable=True,
                status_code=status_code,
                retry_after_seconds=retry_after or 3600,
                key_scoped=True,
            )
        if status_code in {401, 403}:
            return ProviderRequestError(
                provider,
                f"HTTP {status_code}",
                retryable=True,
                status_code=status_code,
                retry_after_seconds=24 * 3600,
                key_scoped=True,
            )
        if status_code in {408, 409, 425} or status_code >= 500:
            return ProviderRequestError(
                provider,
                f"HTTP {status_code}",
                retryable=True,
                status_code=status_code,
                retry_after_seconds=120,
                key_scoped=False,
            )
        return ProviderRequestError(
            provider,
            f"HTTP {status_code}: {body[:240]}",
            retryable=False,
            status_code=status_code,
            retry_after_seconds=600,
            key_scoped=False,
        )

    def _openai_compatible_request(
        self,
        provider: ProviderConfig,
        api_key: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        max_completion_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        base_url = self._normalized_openai_base_url(provider)
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
            "stream": False,
        }
        payload[provider.token_parameter] = max_completion_tokens or provider.max_completion_tokens
        if provider.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": self._schema_name(purpose),
                    "strict": True,
                    "schema": schema,
                },
            }
        elif provider.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if provider.extra_body:
            payload.update(provider.extra_body)
        return f"{base_url}/chat/completions", headers, payload

    def _gemini_request(
        self,
        provider: ProviderConfig,
        api_key: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        max_completion_tokens: int | None,
        temperature: float | None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        system_text = "\n\n".join(item.get("content", "") for item in messages if item.get("role") == "system")
        input_text = "\n\n".join(item.get("content", "") for item in messages if item.get("role") != "system")
        payload: dict[str, Any] = {
            "model": provider.model,
            "system_instruction": system_text,
            "input": input_text,
            "generation_config": {
                "temperature": provider.temperature if temperature is None else temperature,
                "max_output_tokens": max_completion_tokens or provider.max_completion_tokens,
            },
        }
        if provider.response_format in {"json_object", "json_schema"}:
            payload["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            }
        if provider.extra_body:
            payload.update(provider.extra_body)
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
            **(provider.extra_headers or {}),
        }
        return provider.base_url or "https://generativelanguage.googleapis.com/v1beta/interactions", headers, payload

    def _decode_chat_result(
        self,
        raw_text: str,
        usage: dict[str, int | None],
        provider: str,
        model: str,
        response_id: str | None,
    ) -> ProviderResult:
        payload = self._try_loads_json(raw_text)
        reply: NargesReply
        if payload is None:
            clean = self._clean_text_reply(raw_text)
            if not clean or self._looks_like_prompt_echo(clean):
                raise ProviderRequestError(provider, "invalid chat payload", retryable=True)
            reply = NargesReply.from_text(clean)
        else:
            try:
                reply = NargesReply.validate_provider_payload(payload)
            except Exception:
                clean = self._clean_text_reply(raw_text, payload)
                if not clean or self._looks_like_prompt_echo(clean):
                    raise ProviderRequestError(provider, "invalid chat payload", retryable=True)
                reply = NargesReply.from_text(clean)
        if not reply.messages:
            raise ProviderRequestError(provider, "empty chat reply", retryable=True)
        for message in reply.messages:
            message.text = self._sanitize_visible_text(message.text)
        if not any(message.text for message in reply.messages):
            raise ProviderRequestError(provider, "empty visible reply", retryable=True)
        return ProviderResult(
            reply=reply,
            raw_text=raw_text,
            usage=usage,
            provider=provider,
            model=model,
            provider_response_id=response_id,
        )

    def _normalize_narges_state_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload) if isinstance(payload, dict) else {}
        for key, limit in {"mood": 40, "activity": 80, "location": 80, "note": 180, "reason": 240}.items():
            if normalized.get(key) is not None:
                normalized[key] = self._compact_text(str(normalized[key]), limit)
        for key in ("companions", "mind_topics"):
            value = normalized.get(key)
            if isinstance(value, list):
                normalized[key] = [self._compact_text(str(item), 80) for item in value[:8] if str(item).strip()]
        if normalized.get("is_alone") is True:
            normalized["companions"] = []
        if not normalized.get("reason"):
            normalized["reason"] = "scheduled state update"
        return normalized

    def _extract_image_selection_payload(self, payload: dict[str, Any]) -> tuple[str | None, str | None]:
        if not isinstance(payload, dict):
            return None, None
        source = payload.get("image_request") if isinstance(payload.get("image_request"), dict) else payload
        image_id = source.get("image_id") or source.get("id") if isinstance(source, dict) else None
        caption = source.get("caption") or source.get("text") if isinstance(source, dict) else None
        return (
            str(image_id).strip() if image_id else None,
            self._compact_text(str(caption), 500) if caption else None,
        )

    def _extract_text(self, provider: ProviderConfig, data: dict[str, Any]) -> str:
        for key in ("output_text", "text"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
                joined = "\n".join(text for text in texts if text)
                if joined:
                    return joined
        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates:
            parts = ((candidates[0].get("content") or {}).get("parts") or [])
            texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
            joined = "\n".join(text for text in texts if text)
            if joined:
                return joined
        output = data.get("output")
        if isinstance(output, list):
            texts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    texts.extend(str(part.get("text") or "") for part in content if isinstance(part, dict))
            joined = "\n".join(text for text in texts if text)
            if joined:
                return joined
        return ""

    def _extract_model(self, data: dict[str, Any], fallback: str) -> str:
        return str(data.get("model") or fallback)

    def _extract_usage(self, data: dict[str, Any]) -> dict[str, int | None]:
        usage = data.get("usage") or data.get("usageMetadata") or {}
        prompt = self._usage_value(usage, "prompt_tokens", "promptTokenCount", "input_tokens")
        completion = self._usage_value(usage, "completion_tokens", "candidatesTokenCount", "output_tokens")
        total = self._usage_value(usage, "total_tokens", "totalTokenCount")
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    def _usage_value(self, usage: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            if usage.get(key) is None:
                continue
            try:
                return int(usage[key])
            except (TypeError, ValueError):
                continue
        return None

    def _loads_json(self, raw_text: str) -> dict[str, Any]:
        text_value = self._strip_thinking(raw_text).strip()
        text_value = re.sub(r"^```(?:json)?\s*", "", text_value, flags=re.IGNORECASE)
        text_value = re.sub(r"\s*```$", "", text_value)
        try:
            value = json.loads(text_value)
        except json.JSONDecodeError:
            start = text_value.find("{")
            end = text_value.rfind("}")
            if start < 0 or end <= start:
                raise
            value = json.loads(text_value[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("JSON result is not an object")
        return value

    def _try_loads_json(self, raw_text: str) -> dict[str, Any] | None:
        try:
            return self._loads_json(raw_text)
        except Exception:
            return self._repair_partial_json_object(raw_text)

    def _repair_partial_json_object(self, raw_text: str) -> dict[str, Any] | None:
        text_value = self._strip_thinking(raw_text).strip()
        extracted = self._extract_lenient_text_field(text_value)
        if extracted:
            return {"messages": [{"text": extracted}]}
        start = text_value.find("{")
        if start < 0:
            return None
        candidate = text_value[start:]
        if candidate.count('"') % 2 == 1:
            candidate += '"'
        candidate += "]" * max(0, candidate.count("[") - candidate.count("]"))
        candidate += "}" * max(0, candidate.count("{") - candidate.count("}"))
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _clean_text_reply(self, raw_text: str, payload: dict[str, Any] | None = None) -> str:
        if payload:
            for key in ("text", "answer", "message", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return self._sanitize_visible_text(value)
            messages = payload.get("messages")
            if isinstance(messages, list):
                texts = []
                for item in messages[:2]:
                    if isinstance(item, dict):
                        value = item.get("text") or item.get("content")
                        if value:
                            texts.append(str(value))
                    elif isinstance(item, str):
                        texts.append(item)
                if texts:
                    return self._sanitize_visible_text("\n".join(texts))
        text_value = self._strip_thinking(raw_text)
        text_value = re.sub(r"```.*?```", "", text_value, flags=re.DOTALL)
        extracted = self._extract_lenient_text_field(text_value)
        return self._sanitize_visible_text(extracted or text_value)

    def _extract_lenient_text_field(self, text_value: str) -> str | None:
        patterns = (
            r'"messages"\s*:\s*\[\s*\{[^{}]*"(?:text|content)"\s*:\s*"((?:\\.|[^"\\])*)',
            r'"(?:text|answer|message|content|caption)"\s*:\s*"((?:\\.|[^"\\])*)',
        )
        for pattern in patterns:
            match = re.search(pattern, text_value, flags=re.DOTALL)
            if not match:
                continue
            value = match.group(1)
            try:
                return json.loads(f'"{value}"').strip()
            except json.JSONDecodeError:
                return value.replace('\\"', '"').replace("\\n", "\n").strip()
        return None

    def _strip_thinking(self, raw_text: str) -> str:
        text_value = raw_text or ""
        text_value = re.sub(r"<think>.*?</think>", "", text_value, flags=re.DOTALL | re.IGNORECASE)
        text_value = re.sub(r"</?think>", "", text_value, flags=re.IGNORECASE)
        return text_value

    def _sanitize_visible_text(self, text_value: str) -> str:
        clean = self._strip_thinking(text_value)
        clean = re.sub(r"```.*?```", "", clean, flags=re.DOTALL)
        return clean.strip()[:2400]

    def _looks_like_prompt_echo(self, text_value: str) -> bool:
        lowered = (text_value or "").lower()
        markers = (
            "remaining_quota_units_today",
            "user_profile_photos",
            "request_contract",
            "system prompt",
            "runtime_context",
            "compiled_sections",
        )
        return any(marker in lowered for marker in markers)

    def _load_providers(self, settings: Settings) -> list[ProviderConfig]:
        path = Path(settings.ai_providers_config)
        if not path.exists():
            seed_path = Path("config/ai_providers.json")
            if not seed_path.exists():
                raise RuntimeError(f"AI providers config not found: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(seed_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        raw_text = path.read_text(encoding="utf-8-sig")
        self._providers_fingerprint = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        raw = json.loads(raw_text)
        provider_items = raw.get("providers", []) if isinstance(raw, dict) else raw
        configured = [
            self._provider_from_dict(item, index)
            for index, item in enumerate(provider_items)
            if isinstance(item, dict)
        ]
        if not configured:
            raise RuntimeError(f"AI providers config has no providers: {path}")
        return sorted(
            configured,
            key=lambda provider: provider.priority if provider.priority is not None else 10_000,
        )

    def _reload_providers_if_changed(self) -> None:
        if not self._providers_path.exists():
            return
        raw_text = self._providers_path.read_text(encoding="utf-8-sig")
        fingerprint = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        if fingerprint == self._providers_fingerprint:
            return
        self.providers = self._load_providers(self.settings)
        logger.info("ai_provider_config_reloaded path=%s", self._providers_path)

    def _provider_from_dict(self, item: dict[str, Any], index: int = 0) -> ProviderConfig:
        name = str(item.get("name") or f"provider-{index}").strip()
        provider_id = str(item.get("id") or name).strip().lower().replace(" ", "-")
        api_keys = list(item.get("api_keys") or [])
        if item.get("api_key_env"):
            api_keys.insert(0, f"env:{item['api_key_env']}")
        model = item.get("model")
        if not model and isinstance(item.get("models"), list) and item["models"]:
            model = item["models"][0]
        default_token_parameter = "max_tokens" if name.lower() == "nvidia" else "max_completion_tokens"
        return ProviderConfig(
            id=provider_id,
            name=name,
            status_name=provider_id,
            kind=str(item.get("type") or item.get("kind") or "openai_compatible").strip(),
            base_url=str(item.get("base_url") or "").strip() or None,
            api_keys=tuple(str(value).strip() for value in api_keys if str(value).strip()),
            model=str(model or "").strip(),
            priority=int(item["priority"]) if item.get("priority") is not None else None,
            enabled=bool(item.get("enabled", True)),
            temperature=float(item.get("temperature", self.settings.groq_temperature)),
            max_completion_tokens=max(32, int(item.get("max_completion_tokens", self.settings.groq_max_completion_tokens))),
            timeout_seconds=max(5.0, float(item.get("timeout_seconds", 35))),
            response_format=str(item.get("response_format", "json_object")),
            token_parameter=str(item.get("token_parameter", default_token_parameter)),
            extra_headers=dict(item.get("extra_headers") or {}),
            extra_body=dict(item.get("extra_body") or {}),
            prompt_cache=bool(item.get("prompt_cache", item.get("supports_prompt_cache") is True)),
            health_check=bool(item.get("health_check", False)),
            experimental=bool(item.get("experimental", False)),
            use_proxy=bool(item.get("use_proxy", True)),
        )

    def _candidate_providers(self, forced_provider: str | None = None) -> list[ProviderConfig]:
        value = (forced_provider or "").strip().lower()
        if not value:
            return list(self.providers)
        result: list[ProviderConfig] = []
        for index, provider in enumerate(self.providers):
            aliases = {
                provider.id.lower(),
                provider.name.lower(),
                provider.status_name.lower(),
                str(index),
                f"{provider.id}:{provider.model}".lower(),
                f"{provider.name}:{provider.model}".lower(),
            }
            if value in aliases:
                result.append(provider)
        return result

    def _resolve_keys(self, provider: ProviderConfig) -> list[str]:
        keys: list[str] = []
        for raw in provider.api_keys:
            if raw.startswith("env:"):
                value = os.getenv(raw[4:], "").strip()
            elif raw.startswith("$"):
                value = os.getenv(raw[1:], "").strip()
            else:
                value = raw.strip()
            if value and value not in keys:
                keys.append(value)
        return keys

    def _ordered_keys(self, provider_id: str, keys: list[str]) -> list[tuple[int, str]]:
        if not keys:
            return []
        with self._key_cursor_lock:
            start = self._key_cursors.get(provider_id, 0) % len(keys)
        return [((start + offset) % len(keys), keys[(start + offset) % len(keys)]) for offset in range(len(keys))]

    def _advance_key_cursor(self, provider_id: str, key_index: int, key_count: int) -> None:
        if key_count <= 0:
            return
        with self._key_cursor_lock:
            self._key_cursors[provider_id] = (key_index + 1) % key_count

    def _provider_is_healthy(self, provider: ProviderConfig, api_key: str) -> bool:
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]
        cache_key = f"{provider.id}:{key_hash}"
        cached_until = self._health_cache.get(cache_key)
        if cached_until and cached_until > datetime.now(UTC):
            return True
        if provider.kind == "gemini_interactions":
            return True
        base_url = self._normalized_openai_base_url(provider)
        if not base_url:
            return False
        try:
            response = self._client_for_provider(provider).get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}", **(provider.extra_headers or {})},
                timeout=min(provider.timeout_seconds, 6),
            )
        except httpx.HTTPError:
            return False
        if response.status_code in {401, 403}:
            return False
        if response.status_code >= 500:
            return False
        self._health_cache[cache_key] = datetime.now(UTC) + timedelta(minutes=5)
        return True

    def _normalized_openai_base_url(self, provider: ProviderConfig) -> str:
        base_url = (provider.base_url or "").rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)].rstrip("/")
        return base_url

    def _client_for_provider(self, provider: ProviderConfig) -> httpx.Client:
        return self.http_client if provider.use_proxy else self.direct_http_client

    def _provider_is_temporarily_disabled(self, provider_id: str) -> bool:
        until = self._provider_disabled_until.get(provider_id)
        if until is None:
            return False
        if until <= datetime.now(UTC):
            self._provider_disabled_until.pop(provider_id, None)
            return False
        return True

    def _disable_provider_temporarily(self, provider_id: str, error: ProviderRequestError) -> None:
        seconds = error.retry_after_seconds or (60 if error.retryable else 600)
        self._provider_disabled_until[provider_id] = datetime.now(UTC) + timedelta(seconds=min(seconds, 900))

    def _key_is_temporarily_disabled(self, provider: str, key_index: int) -> bool:
        if self.database is None:
            return False
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(AiProviderKeyStatusORM, {"provider": provider, "key_index": key_index})
            if row is None or row.disabled_until is None:
                return False
            disabled_until = self._as_utc(row.disabled_until)
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
        with self.database.orm.session() as session:
            row = session.get(AiProviderKeyStatusORM, {"provider": provider, "key_index": key_index})
            if row is None:
                row = AiProviderKeyStatusORM(provider=provider, key_index=key_index, status="failed", updated_at=now)
                session.add(row)
            next_count = int(row.error_count or 0) + 1
            disabled_seconds = exc.retry_after_seconds
            if disabled_seconds is None and next_count >= 3:
                disabled_seconds = 120 if exc.retryable else 900
            row.status = "limited" if exc.status_code == 429 else "failed"
            row.error_count = next_count
            row.last_error = str(exc)[:500]
            row.disabled_until = now + timedelta(seconds=disabled_seconds) if disabled_seconds else None
            row.updated_at = now

    def _retry_after(self, response: httpx.Response) -> int | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return 3600 if response.status_code == 429 else None
        try:
            return max(10, min(int(float(raw)), 24 * 3600))
        except ValueError:
            return 3600

    def _log_ai_request(self, purpose: str, provider: ProviderConfig, key_index: int, payload: dict[str, Any]) -> None:
        messages = payload.get("messages")
        input_chars = 0
        role_chars: dict[str, int] = {}
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                chars = len(str(item.get("content") or ""))
                input_chars += chars
                role = str(item.get("role") or "unknown")
                role_chars[role] = role_chars.get(role, 0) + chars
        else:
            input_chars = len(str(payload.get("input") or "")) + len(str(payload.get("system_instruction") or ""))
        logger.info(
            "AI_REQUEST %s",
            json.dumps(
                {
                    "purpose": purpose,
                    "provider": provider.id,
                    "model": provider.model,
                    "key_index": key_index,
                    "input_chars": input_chars,
                    "input_chars_by_role": role_chars,
                    "max_output_tokens": payload.get(provider.token_parameter)
                    or (payload.get("generation_config") or {}).get("max_output_tokens"),
                    "structured": "response_format" in payload,
                },
                ensure_ascii=False,
            ),
        )

    def _log_ai_response(
        self,
        purpose: str,
        provider: ProviderConfig,
        raw_text: str,
        usage: dict[str, int | None],
        latency_ms: int,
    ) -> None:
        logger.info(
            "AI_RESPONSE %s",
            json.dumps(
                {
                    "purpose": purpose,
                    "provider": provider.id,
                    "model": provider.model,
                    "output_chars": len(raw_text),
                    "usage": usage,
                    "latency_ms": latency_ms,
                },
                ensure_ascii=False,
            ),
        )

    def _log_ai_error(self, purpose: str, provider: ProviderConfig, exc: Exception, status_code: int | None) -> None:
        logger.warning(
            "AI_ERROR %s",
            json.dumps(
                {
                    "purpose": purpose,
                    "provider": provider.id,
                    "model": provider.model,
                    "status_code": status_code,
                    "error": exc.__class__.__name__,
                    "message": str(exc)[:300],
                },
                ensure_ascii=False,
            ),
        )

    def _schema_name(self, purpose: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_-]+", "_", purpose or "response")
        return value[:48] or "response"

    def _compact_text(self, value: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", (value or "").strip())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"

    def _as_utc(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
