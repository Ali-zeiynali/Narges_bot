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
        "relationship_note",
        "inside_joke",
        "boundary",
        "unresolved_topic",
        "temporary_event",
    ]
    summary: str = Field(min_length=3, max_length=240)
    confidence: float = Field(ge=0, le=1)
    importance: int = Field(default=3, ge=1, le=5)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class RelationshipDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    familiarity: int = Field(default=0, ge=-3, le=3)
    trust: int = Field(default=0, ge=-3, le=3)
    respect: int = Field(default=0, ge=-3, le=3)
    comfort: int = Field(default=0, ge=-3, le=3)
    joke_permission: bool | None = None
    nickname: str | None = Field(default=None, max_length=32)
    boundary_warning: str | None = Field(default=None, max_length=160)
    intimacy_delta: int = Field(default=0, ge=-1, le=1)
    current_chat_feeling: str | None = Field(default=None, max_length=40)


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
    relationship_delta: RelationshipDelta = Field(default_factory=RelationshipDelta)
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
