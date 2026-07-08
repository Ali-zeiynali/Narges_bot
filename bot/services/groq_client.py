from __future__ import annotations

import json
import logging
import os
import re
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
    name: str
    kind: str
    model: str
    api_keys: tuple[str, ...]
    priority: int | None = None
    base_url: str | None = None
    enabled: bool = True
    temperature: float = 0.7
    max_completion_tokens: int = 512
    timeout_seconds: float = 45
    response_format: str = "json_object"
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    prompt_cache: bool = False
    health_check: bool = False
    experimental: bool = False
    use_proxy: bool = True


class ProviderRequestError(RuntimeError):
    def __init__(self, provider: str, message: str, retryable: bool = True) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable


class GroqChatClient:
    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.database = database
        self._providers_path = Path(settings.ai_providers_config)
        self._providers_fingerprint: tuple[int, int] | None = None
        self.providers = self._load_providers(settings)
        self._health_cache: dict[str, datetime] = {}
        self.proxy_url = settings.groq_proxy or settings.telegram_proxy
        self.http_client = httpx.Client(proxy=self.proxy_url, timeout=45) if self.proxy_url else httpx.Client(timeout=45)
        self.direct_http_client = httpx.Client(timeout=45)

    def complete(self, messages: list[dict[str, str]]) -> GroqResult:
        raw_text, usage, provider, model = self._complete_json(messages, NargesReply.model_json_schema(), "chat")
        payload = self._try_loads_json(raw_text)
        if payload is None:
            reply = NargesReply.from_text(self._clean_text_reply(raw_text))
        else:
            try:
                reply = NargesReply.validate_provider_payload(payload)
            except Exception:
                logger.exception("provider_payload_validation_failed raw_text=%s", raw_text[:1000])
                reply = NargesReply.from_text(self._clean_text_reply(raw_text, payload))
        return GroqResult(
            reply=reply,
            raw_text=raw_text,
            usage=usage,
            provider=provider,
            model=model,
        )

    def complete_conversation_summary(self, existing_summary: str, messages: list[dict[str, str]]) -> str:
        prompt_payload = {
            "existing_summary": existing_summary,
            "new_messages": [
                {
                    "role": item.get("role"),
                    "text": item.get("text"),
                    "created_at": item.get("created_at"),
                    "intent": item.get("intent"),
                }
                for item in messages
            ],
            "rules": [
                "Write a compact factual memory summary for future context.",
                "Do not imitate the user's or assistant's tone.",
                "Do not include raw dialogue unless it is a stable fact or unresolved topic.",
                "Keep it under 900 characters.",
            ],
        }
        raw_text, _usage, _provider, _model = self._complete_json(
            [
                {
                    "role": "system",
                    "content": "You update durable conversation summaries. Return only JSON: {\"summary\":\"...\"}.",
                },
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ],
            {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
            "conversation_summary",
            max_completion_tokens=260,
            temperature=0.2,
        )
        payload = self._try_loads_json(raw_text) or {}
        summary = payload.get("summary")
        return str(summary or raw_text).strip()

    def complete_narges_state(self, messages: list[dict[str, str]]) -> tuple[NargesSelfStateCandidate, dict[str, int | None]]:
        raw_text, usage, _provider, _model = self._complete_json(
            messages,
            NargesSelfStateCandidate.model_json_schema(),
            "narges_state",
            max_completion_tokens=500,
            temperature=0.8,
        )
        payload = self._loads_json(raw_text)
        payload = self._normalize_narges_state_payload(payload)
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
                "caption": {"type": ["string", "null"]},
            },
            "required": ["image_id", "caption"],
            "additionalProperties": False,
        }
        selection_payload = {
            "task": "Strictly decide whether one local catalog image should be attached to this Telegram reply.",
            "rules": [
                "Return image_id null unless the original assistant explicitly requested an image and the catalog has a fitting image.",
                "Use only an id from image_catalog; never invent ids.",
                "Return image_id null for vague, indirect, joking, or weak photo requests.",
                "Return image_id null if the selected image would not clearly match image_request.prompt and the conversation context.",
                "Caption must be short Persian text suitable for Telegram and must not claim details not supported by the catalog description.",
            ],
            "image_request": image_request,
            "image_catalog": image_catalog,
            "previous_prompt": original_messages,
        }
        raw_text, usage, provider, model = self._complete_json(
            [
                {
                    "role": "system",
                    "content": "You are a strict local image catalog gate. Return only compact JSON with image_id and caption.",
                },
                {"role": "user", "content": json.dumps(selection_payload, ensure_ascii=False)},
            ],
            schema,
            "image_selection",
            max_completion_tokens=180,
            temperature=0.2,
        )
        payload = self._try_loads_json(raw_text) or {}
        image_id, caption = self._extract_image_selection_payload(payload)
        return ImageSelectionResult(
            image_id=str(image_id).strip() if image_id else None,
            caption=str(caption).strip()[:900] if caption else None,
            usage=usage,
            provider=provider,
            model=model,
        )

    def _normalize_narges_state_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        normalized = dict(payload)
        text_limits = {
            "mood": 40,
            "activity": 80,
            "location": 80,
            "note": 180,
            "reason": 240,
        }
        for key, limit in text_limits.items():
            if normalized.get(key) is not None:
                normalized[key] = str(normalized[key]).strip()[:limit]
        for key, limit in {"companions": 80, "mind_topics": 80}.items():
            value = normalized.get(key)
            if isinstance(value, list):
                normalized[key] = [str(item).strip()[:limit] for item in value if str(item).strip()]
        if normalized.get("is_alone") is True:
            normalized["companions"] = []
        if not normalized.get("reason"):
            normalized["reason"] = "scheduled state update"
        return normalized

    def _extract_image_selection_payload(self, payload: dict[str, Any]) -> tuple[Any, Any]:
        image_id = payload.get("image_id")
        caption = payload.get("caption")
        if image_id:
            return image_id, caption
        image_request = payload.get("image_request")
        if isinstance(image_request, dict):
            image_id = image_request.get("image_id") or image_request.get("id")
            caption = caption or image_request.get("caption") or image_request.get("text")
            if image_id:
                return image_id, caption
        messages = payload.get("messages")
        if isinstance(messages, list):
            for item in messages:
                if not isinstance(item, dict):
                    continue
                image_id = item.get("image_id") or item.get("photo_id")
                caption = caption or item.get("text") or item.get("caption")
                if image_id:
                    return image_id, caption
        return image_id, caption

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
        self._reload_providers_if_changed()
        for provider in self.providers:
            if not provider.enabled:
                continue
            keys = self._resolve_keys(provider)
            if not keys:
                continue
            provider_failures = 0
            for key_index, api_key in enumerate(keys):
                if self._key_is_temporarily_disabled(provider.name, key_index):
                    continue
                if provider.experimental and provider.health_check and not self._provider_is_healthy(provider, api_key):
                    last_error = ProviderRequestError(provider.name, "health check failed", retryable=True)
                    self._record_key_failure(provider.name, key_index, last_error)
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
                    provider_failures += 1
                    self._record_key_failure(provider.name, key_index, exc)
                    if not exc.retryable:
                        break
                    if provider_failures >= 1:
                        logger.warning("ai_provider_failed provider=%s moving_to_next error=%s", provider.name, exc)
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
            url, headers, payload = self._gemini_request(provider, api_key, messages, schema, purpose, max_completion_tokens, temperature)
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
        response = self._post_with_fallbacks(provider, url, headers, payload, purpose)
        if response.status_code >= 400:
            retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
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
            "max_completion_tokens": max_completion_tokens or provider.max_completion_tokens,
            "stream": False,
            **(provider.extra_body or {}),
        }
        if purpose != "chat" and provider.response_format in {"json_object", "json_schema"}:
            payload["response_format"] = {"type": "json_object"}
        return f"{base_url}/chat/completions", headers, payload

    def _normalized_openai_base_url(self, provider: ProviderConfig) -> str:
        base_url = (provider.base_url or "").rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)].rstrip("/")
        return base_url

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
                response = self._client_for_provider(provider).post(url, headers=headers, json=variant, timeout=provider.timeout_seconds)
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
        purpose: str,
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
            **(provider.extra_body or {}),
        }
        if purpose != "chat":
            payload["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
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
        prompt_tokens = self._usage_value(usage, "prompt_tokens", "promptTokenCount")
        completion_tokens = self._usage_value(usage, "completion_tokens", "candidatesTokenCount", "output_tokens")
        total_tokens = self._usage_value(usage, "total_tokens", "totalTokenCount")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
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

    def _try_loads_json(self, raw_text: str) -> dict[str, Any] | None:
        try:
            payload = self._loads_json(raw_text)
        except Exception:
            payload = self._repair_partial_json_object(raw_text)
            if payload is None:
                return None
        return payload if isinstance(payload, dict) else None

    def _clean_text_reply(self, raw_text: str, payload: dict[str, Any] | None = None) -> str:
        if payload:
            for key in ("text", "answer", "message", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            messages = payload.get("messages")
            if isinstance(messages, list) and messages:
                first = messages[0]
                if isinstance(first, dict):
                    value = first.get("text") or first.get("content")
                    if value:
                        return str(value).strip()
                if isinstance(first, str):
                    return first.strip()
        text = raw_text.strip()
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        extracted = self._extract_lenient_text_field(text)
        if extracted:
            return extracted
        if text.startswith("{") and text.endswith("}"):
            text = text.strip("{}").replace('"', "").strip()
        return text or "الان جوابم درست آماده نشد. یک بار کوتاه‌تر بفرست."

    def _repair_partial_json_object(self, raw_text: str) -> dict[str, Any] | None:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        start = text.find("{")
        if start < 0:
            extracted = self._extract_lenient_text_field(text)
            return {"messages": [{"text": extracted}]} if extracted else None
        text = text[start:]
        candidates: list[str] = []
        end = text.rfind("}")
        if end > 0:
            candidates.append(text[: end + 1])
        repaired = text
        if repaired.count('"') % 2 == 1:
            repaired += '"'
        repaired += "]" * max(0, repaired.count("[") - repaired.count("]"))
        repaired += "}" * max(0, repaired.count("{") - repaired.count("}"))
        candidates.append(repaired)
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        extracted = self._extract_lenient_text_field(text)
        return {"messages": [{"text": extracted}]} if extracted else None

    def _extract_lenient_text_field(self, text: str) -> str | None:
        patterns = (
            r'"messages"\s*:\s*\[\s*\{[^{}]*"(?:text|content)"\s*:\s*"((?:\\.|[^"\\])*)',
            r'"(?:text|answer|message|content|caption)"\s*:\s*"((?:\\.|[^"\\])*)',
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.DOTALL)
            if not match:
                continue
            value = match.group(1)
            try:
                return json.loads(f'"{value}"').strip()
            except json.JSONDecodeError:
                return value.replace('\\"', '"').replace("\\n", "\n").strip()
        return None

    def _load_providers(self, settings: Settings) -> list[ProviderConfig]:
        path = Path(settings.ai_providers_config)
        if not path.exists():
            seed_path = Path("config/ai_providers.json")
            if seed_path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(seed_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
                logger.info("ai_provider_config_seeded source=%s target=%s", seed_path, path)
            else:
                raise RuntimeError(f"AI providers config not found: {path}")
        raw_text = path.read_text(encoding="utf-8-sig")
        stat = path.stat()
        self._providers_fingerprint = (stat.st_mtime_ns, hash(raw_text))
        raw = json.loads(raw_text)
        providers = raw.get("providers", raw if isinstance(raw, list) else [])
        configured = [
            self._provider_from_dict(item, index)
            for index, item in enumerate(providers)
            if isinstance(item, dict)
        ]
        if not configured:
            raise RuntimeError(f"AI providers config has no providers: {path}")
        ordered = sorted(
            enumerate(configured),
            key=lambda pair: pair[1].priority if pair[1].priority is not None else 10_000 + pair[0],
        )
        return [provider for _index, provider in ordered]

    def _reload_providers_if_changed(self) -> None:
        path = self._providers_path
        if not path.exists():
            return
        raw_text = path.read_text(encoding="utf-8-sig")
        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, hash(raw_text))
        if fingerprint == self._providers_fingerprint:
            return
        logger.info("ai_provider_config_reloaded path=%s", path)
        self.providers = self._load_providers(self.settings)

    def _provider_from_dict(self, item: dict[str, Any], index: int = 0) -> ProviderConfig:
        api_keys = list(item.get("api_keys", []))
        if item.get("api_key_env"):
            api_keys.insert(0, f"env:{item['api_key_env']}")
        model = item.get("model")
        if not model and isinstance(item.get("models"), list) and item["models"]:
            model = item["models"][0]
        return ProviderConfig(
            name=str(item["name"]).strip(),
            kind=str(item.get("type") or item.get("kind") or "openai_compatible").strip(),
            base_url=str(item.get("base_url") or "").strip() or None,
            api_keys=tuple(str(value).strip() for value in api_keys if str(value).strip()),
            model=str(model or "").strip(),
            priority=int(item["priority"]) if item.get("priority") is not None else None,
            enabled=bool(item.get("enabled", True)),
            temperature=float(item.get("temperature", self.settings.groq_temperature)),
            max_completion_tokens=max(
                int(item.get("max_completion_tokens", self.settings.groq_max_completion_tokens)),
                self.settings.groq_max_completion_tokens,
            ),
            timeout_seconds=float(item.get("timeout_seconds", 45)),
            response_format=str(item.get("response_format", "json_object")),
            extra_headers=dict(item.get("extra_headers") or {}),
            extra_body=dict(item.get("extra_body") or {}),
            prompt_cache=bool(item.get("prompt_cache", item.get("supports_prompt_cache") is True)),
            health_check=bool(item.get("health_check", False)),
            experimental=bool(item.get("experimental", False)),
            use_proxy=bool(item.get("use_proxy", True)),
        )

    def _client_for_provider(self, provider: ProviderConfig) -> httpx.Client:
        return self.http_client if provider.use_proxy else self.direct_http_client

    def _provider_is_healthy(self, provider: ProviderConfig, api_key: str) -> bool:
        cache_key = f"{provider.name}:{provider.base_url}"
        cached_until = self._health_cache.get(cache_key)
        if cached_until and cached_until > datetime.now(UTC):
            return True
        base_url = self._normalized_openai_base_url(provider)
        if not base_url:
            return False
        try:
            response = self._client_for_provider(provider).get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}", **(provider.extra_headers or {})},
                timeout=min(provider.timeout_seconds, 8),
            )
        except httpx.HTTPError:
            logger.warning("ai_provider_health_check_failed provider=%s", provider.name)
            return False
        if response.status_code >= 400:
            if response.status_code in {401, 403, 404, 405}:
                logger.info("ai_provider_health_endpoint_inconclusive provider=%s status=%s", provider.name, response.status_code)
                self._health_cache[cache_key] = datetime.now(UTC) + timedelta(minutes=5)
                return True
            logger.warning("ai_provider_health_check_bad_status provider=%s status=%s", provider.name, response.status_code)
            return False
        self._health_cache[cache_key] = datetime.now(UTC) + timedelta(minutes=5)
        return True

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
