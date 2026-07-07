from enum import Enum
from typing import Any, Literal

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


class ConversationState(str, Enum):
    NORMAL = "normal"
    SEXUAL = "sexual"


class TelegramOutboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1200)
    delay_seconds: float = Field(default=0.4, ge=0, le=8)
    image_id: str | None = Field(default=None, max_length=128)

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
    memory_id: int | None = Field(default=None, ge=1)
    kind: Literal[
        "identity",
        "fact",
        "preference",
        "project",
        "goal",
        "constraint",
        "user_state",
        "interaction_style",
        "inside_joke",
        "boundary",
        "unresolved_topic",
        "temporary_event",
    ]
    summary: str = Field(min_length=1, max_length=600)
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


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needed: bool = False
    reason: str | None = Field(default=None, max_length=240)
    prompt: str | None = Field(default=None, max_length=300)
    caption: str | None = Field(default=None, max_length=800)


class NargesReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ResponseMode
    conversation_state: ConversationState = ConversationState.NORMAL
    messages: list[TelegramOutboundMessage] = Field(min_length=1, max_length=4)
    memory_suggestions: list[MemorySuggestion] = Field(default_factory=list, max_length=12)
    warning_suggestion: WarningSuggestion | None = None
    event_suggestion: EventSuggestion | None = None
    image_request: ImageRequest | None = None

    @model_validator(mode="after")
    def validate_message_batch(self) -> "NargesReply":
        texts = [message.text.strip() for message in self.messages]
        if len(texts) != len(set(texts)):
            raise ValueError("duplicate telegram messages are not allowed")
        if len(texts) > 2 and sum(len(text) < 12 for text in texts) >= 2:
            raise ValueError("dramatic short message sequences are not allowed")
        return self

    @classmethod
    def from_text(cls, text: str) -> "NargesReply":
        return cls.model_validate(
            {
                "mode": "normal",
                "conversation_state": "normal",
                "messages": [{"text": cls._trim_message_text(text), "delay_seconds": 0.4}],
                "memory_suggestions": [],
                "warning_suggestion": None,
                "event_suggestion": None,
            }
        )

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
        if normalized.get("conversation_state") not in ConversationState._value2member_map_:
            normalized["conversation_state"] = "normal"

        messages = normalized.get("messages")
        if not messages:
            text = normalized.get("text") or normalized.get("answer") or normalized.get("message") or normalized.get("content")
            if text:
                messages = [{"text": str(text), "delay_seconds": normalized.get("delay_seconds", 0.4)}]
        if not isinstance(messages, list) or not messages:
            messages = [{"text": "الان جوابم درست آماده نشد. یک بار کوتاه‌تر بفرست.", "delay_seconds": 0.2}]

        cleaned_messages = []
        for item in messages[:4]:
            if isinstance(item, dict):
                cleaned_messages.append(
                    {
                        "text": cls._trim_message_text(str(item.get("text") or item.get("content") or "")),
                        "delay_seconds": item.get("delay_seconds", 0.4),
                        "image_id": item.get("image_id") or item.get("photo_id"),
                    }
                )
            else:
                cleaned_messages.append({"text": cls._trim_message_text(str(item)), "delay_seconds": 0.4})
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

        normalized["memory_suggestions"] = cls._normalize_memory_suggestions(
            normalized.get("memory_suggestions") or normalized.get("memory") or normalized.get("memories")
        )
        if not isinstance(normalized.get("event_suggestion"), dict):
            normalized["event_suggestion"] = None
        image_request = normalized.get("image_request") or normalized.get("photo_request")
        if isinstance(image_request, dict):
            normalized["image_request"] = {
                "needed": bool(image_request.get("needed", True)),
                "reason": image_request.get("reason"),
                "prompt": image_request.get("prompt") or image_request.get("description"),
                "caption": image_request.get("caption") or image_request.get("text"),
            }
        elif image_request:
            normalized["image_request"] = {"needed": True, "prompt": str(image_request)[:300]}
        else:
            normalized["image_request"] = None
        allowed = set(cls.model_fields)
        return {key: value for key, value in normalized.items() if key in allowed}

    @classmethod
    def _normalize_memory_suggestions(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        cleaned: list[dict[str, Any]] = []
        allowed_kinds = {
            "identity",
            "fact",
            "preference",
            "project",
            "goal",
            "constraint",
            "user_state",
            "interaction_style",
            "inside_joke",
            "boundary",
            "unresolved_topic",
            "temporary_event",
        }
        allowed_actions = {"create", "save", "edit", "merge", "replace", "delete", "forget"}
        for item in value[:5]:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or item.get("text") or item.get("value") or "").strip()
            if not summary:
                continue
            action = str(item.get("action") or "create").strip().lower()
            kind = str(item.get("kind") or cls._infer_memory_kind(summary)).strip().lower()
            try:
                confidence = float(item.get("confidence", 0.75))
            except (TypeError, ValueError):
                confidence = 0.75
            try:
                importance = int(item.get("importance", 3))
            except (TypeError, ValueError):
                importance = 3
            suggestion: dict[str, Any] = {
                "action": action if action in allowed_actions else "create",
                "kind": kind if kind in allowed_kinds else "preference",
                "summary": summary[:600],
                "confidence": max(0, min(confidence, 1)),
                "importance": max(1, min(importance, 5)),
            }
            if item.get("memory_id") is not None or item.get("id") is not None:
                try:
                    memory_id = int(item.get("memory_id") or item.get("id"))
                except (TypeError, ValueError):
                    memory_id = None
                if memory_id and memory_id > 0:
                    suggestion["memory_id"] = memory_id
            if item.get("expires_in_days") is not None:
                try:
                    expires_in_days = int(item.get("expires_in_days"))
                except (TypeError, ValueError):
                    expires_in_days = None
                if expires_in_days is not None:
                    suggestion["expires_in_days"] = max(1, min(expires_in_days, 365))
            cleaned.append(suggestion)
        return cleaned

    @classmethod
    def _infer_memory_kind(cls, summary: str) -> str:
        lowered = summary.lower()
        if any(word in lowered for word in ("like", "love", "prefer", "دوست", "خوشم", "ترجیح")):
            return "preference"
        if any(word in lowered for word in ("call me", "name", "اسم", "صدام")):
            return "identity"
        if any(word in lowered for word in ("project", "پروژه")):
            return "project"
        if any(word in lowered for word in ("goal", "هدف")):
            return "goal"
        return "preference"

    @classmethod
    def _trim_message_text(cls, text: str) -> str:
        lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
        text = "\n".join(lines[:8]).strip()
        if len(text) > 1200:
            text = text[:1190].rstrip() + "..."
        return text or "الان جوابم درست آماده نشد. یک بار کوتاه‌تر بفرست."
