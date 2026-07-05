import json
from dataclasses import dataclass
from typing import Any

from groq import Groq

from bot.config import Settings
from bot.models.ai import NargesReply
from bot.models.state import NargesSelfStateCandidate


@dataclass(frozen=True)
class GroqResult:
    reply: NargesReply
    raw_text: str
    usage: dict[str, int | None]


class GroqChatClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = Groq(api_key=settings.groq_api_key)

    def complete(self, messages: list[dict[str, str]]) -> GroqResult:
        completion = self.client.chat.completions.create(
            model=self.settings.groq_model,
            messages=messages,
            temperature=self.settings.groq_temperature,
            max_completion_tokens=self.settings.groq_max_completion_tokens,
            response_format={"type": "json_object"},
        )
        raw_text = completion.choices[0].message.content or "{}"
        payload = json.loads(raw_text)
        return GroqResult(
            reply=NargesReply.model_validate(payload),
            raw_text=raw_text,
            usage=self._extract_usage(completion.usage),
        )

    def complete_narges_state(self, messages: list[dict[str, str]]) -> tuple[NargesSelfStateCandidate, dict[str, int | None]]:
        completion = self.client.chat.completions.create(
            model=self.settings.groq_model,
            messages=messages,
            temperature=0.8,
            max_completion_tokens=500,
            response_format={"type": "json_object"},
        )
        raw_text = completion.choices[0].message.content or "{}"
        payload = json.loads(raw_text)
        return NargesSelfStateCandidate.model_validate(payload), self._extract_usage(completion.usage)

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
