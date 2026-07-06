from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResponseMode(str, Enum):
    SHORT = "short"
    NORMAL = "normal"
    PLAYFUL = "playful"
    SERIOUS = "serious"
    SUPPORTIVE = "supportive"
    DETAILED = "detailed"
    DEEP = "deep"
    UPSET = "upset"
    COLD = "cold"


class TelegramOutboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1200)
    delay_seconds: float = Field(default=0.4, ge=0, le=8)

    @field_validator("text")
    @classmethod
    def text_must_be_real(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message text cannot be empty")
        if len(value.splitlines()) > 8:
            raise ValueError("telegram message cannot be longer than 8 lines")
        return value


class MemorySuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "save", "edit", "merge", "replace", "delete", "forget"]
    kind: Literal[
        "identity",
        "preference",
        "project",
        "goal",
        "constraint",
        "inside_joke",
        "boundary",
        "unresolved_topic",
        "temporary_event",
    ]
    summary: str = Field(min_length=3, max_length=240)
    confidence: float = Field(ge=0, le=1)
    importance: int = Field(default=3, ge=1, le=5)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class WarningSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["none", "soft", "firm"] = "none"
    reason: str | None = Field(default=None, max_length=160)


class EventSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=80)
    reason: str = Field(min_length=3, max_length=160)
    duration_minutes: int = Field(ge=15, le=180)


class NargesReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ResponseMode
    messages: list[TelegramOutboundMessage] = Field(min_length=1, max_length=4)
    memory_suggestions: list[MemorySuggestion] = Field(default_factory=list, max_length=5)
    warning_suggestion: WarningSuggestion | None = None
    event_suggestion: EventSuggestion | None = None

    @model_validator(mode="after")
    def validate_message_batch(self) -> "NargesReply":
        texts = [message.text.strip() for message in self.messages]
        if len(texts) != len(set(texts)):
            raise ValueError("duplicate telegram messages are not allowed")
        if len(texts) > 2 and sum(len(text) < 12 for text in texts) >= 2:
            raise ValueError("dramatic short message sequences are not allowed")
        return self

    @classmethod
    def validate_provider_payload(cls, payload: dict) -> "NargesReply":
        normalized = cls._normalize_payload(payload)
        return cls.model_validate(normalized)

    @classmethod
    def _normalize_payload(cls, payload: dict) -> dict:
        if not isinstance(payload, dict):
            payload = {}
        normalized = dict(payload)
        if "mode" not in normalized:
            tone = str(normalized.get("tone") or "").lower()
            depth = str(normalized.get("depth") or "").lower()
            normalized["mode"] = tone if tone in ResponseMode._value2member_map_ else depth or "normal"
        if normalized.get("mode") not in ResponseMode._value2member_map_:
            normalized["mode"] = "normal"

        messages = normalized.get("messages")
        if not isinstance(messages, list) or not messages:
            normalized["messages"] = [{"text": "الان جوابم درست آماده نشد. یک بار کوتاه‌تر بفرست.", "delay_seconds": 0.2}]
        else:
            cleaned_messages = []
            for item in messages[:4]:
                if isinstance(item, dict):
                    cleaned_messages.append(
                        {
                            "text": str(item.get("text") or item.get("content") or "").strip(),
                            "delay_seconds": item.get("delay_seconds", 0.4),
                        }
                    )
                else:
                    cleaned_messages.append({"text": str(item).strip(), "delay_seconds": 0.4})
            normalized["messages"] = [item for item in cleaned_messages if item["text"]] or [
                {"text": "الان جوابم درست آماده نشد. یک بار کوتاه‌تر بفرست.", "delay_seconds": 0.2}
            ]

        warning = normalized.get("warning_suggestion")
        if isinstance(warning, dict):
            normalized["warning_suggestion"] = {
                "level": warning.get("level", "none"),
                "reason": warning.get("reason") or warning.get("text"),
            }
        elif warning:
            normalized["warning_suggestion"] = {"level": "soft", "reason": str(warning)[:160]}
        else:
            normalized["warning_suggestion"] = None

        if not isinstance(normalized.get("memory_suggestions"), list):
            normalized["memory_suggestions"] = []
        if not isinstance(normalized.get("event_suggestion"), dict):
            normalized["event_suggestion"] = None
        allowed = set(cls.model_fields)
        return {key: value for key, value in normalized.items() if key in allowed}
