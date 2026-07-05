import json
import logging
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from groq import Groq

from bot.config import Settings
from bot.models.ai import NargesReply
from bot.models.state import NargesSelfStateCandidate


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroqResult:
    reply: NargesReply
    raw_text: str
    usage: dict[str, int | None]


class GroqChatClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http_client = httpx.Client(proxy=settings.groq_proxy) if settings.groq_proxy else None
        self.client = Groq(api_key=settings.groq_api_key, http_client=self.http_client)

    def complete(self, messages: list[dict[str, str]]) -> GroqResult:
        request_payload = {
            "model": self.settings.groq_model,
            "messages": messages,
            "temperature": self.settings.groq_temperature,
            "max_completion_tokens": self.settings.groq_max_completion_tokens,
            "response_format": {"type": "json_object"},
        }
        self._log_ai_request("chat", request_payload)
        try:
            completion = self.client.chat.completions.create(**request_payload)
            raw_text = completion.choices[0].message.content or "{}"
            self._log_ai_response("chat", raw_text, completion.usage)
            payload = json.loads(raw_text)
            return GroqResult(
                reply=NargesReply.model_validate(payload),
                raw_text=raw_text,
                usage=self._extract_usage(completion.usage),
            )
        except Exception as exc:
            self._log_ai_error("chat", request_payload, exc)
            raise

    def complete_narges_state(self, messages: list[dict[str, str]]) -> tuple[NargesSelfStateCandidate, dict[str, int | None]]:
        request_payload = {
            "model": self.settings.groq_model,
            "messages": messages,
            "temperature": 0.8,
            "max_completion_tokens": 500,
            "response_format": {"type": "json_object"},
        }
        self._log_ai_request("narges_state", request_payload)
        try:
            completion = self.client.chat.completions.create(**request_payload)
            raw_text = completion.choices[0].message.content or "{}"
            self._log_ai_response("narges_state", raw_text, completion.usage)
            payload = json.loads(raw_text)
            return NargesSelfStateCandidate.model_validate(payload), self._extract_usage(completion.usage)
        except Exception as exc:
            self._log_ai_error("narges_state", request_payload, exc)
            raise

    def _extract_usage(self, usage: Any) -> dict[str, int | None]:
        return {
            "prompt_tokens": self._usage_value(usage, "prompt_tokens"),
            "completion_tokens": self._usage_value(usage, "completion_tokens"),
            "total_tokens": self._usage_value(usage, "total_tokens"),
        }

    def _usage_value(self, usage: Any, key: str) -> int | None:
        if usage is None:
            return None
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)

    def _log_ai_request(self, purpose: str, payload: dict[str, Any]) -> None:
        line = "AI_REQUEST " + json.dumps({"purpose": purpose, "payload": payload}, ensure_ascii=False, default=str)
        logger.info(line)
        self._safe_print(line)

    def _log_ai_response(self, purpose: str, raw_text: str, usage: Any) -> None:
        line = "AI_RESPONSE " + json.dumps(
            {"purpose": purpose, "raw_text": raw_text, "usage": self._extract_usage(usage)},
            ensure_ascii=False,
            default=str,
        )
        logger.info(line)
        self._safe_print(line)

    def _log_ai_error(self, purpose: str, payload: dict[str, Any], exc: Exception) -> None:
        line = "AI_ERROR " + json.dumps(
            {"purpose": purpose, "payload": payload, "error": exc.__class__.__name__, "message": str(exc)},
            ensure_ascii=False,
            default=str,
        )
        logger.exception(line)
        self._safe_print(line)

    def _safe_print(self, line: str) -> None:
        try:
            print(line)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((line + "\n").encode("utf-8"))
            sys.stdout.flush()
