from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DailyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=3, max_length=80)
    activity: str = Field(min_length=3, max_length=80)
    location: str = Field(min_length=2, max_length=80)
    topic: str | None = Field(default=None, max_length=80)
    start_at: datetime
    end_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> "DailyEvent":
        if self.end_at <= self.start_at:
            raise ValueError("event end must be after start")
        if self.expires_at < self.end_at:
            raise ValueError("event expiry must be after end")
        return self


class GlobalState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mood: str = "normal"
    energy: int = Field(default=70, ge=0, le=100)
    patience: int = Field(default=70, ge=0, le=100)
    playfulness: int = Field(default=45, ge=0, le=100)
    activity: str = "available"
    location: str = "home"
    current_topics: list[str] = Field(default_factory=list, max_length=8)
    active_events: list[DailyEvent] = Field(default_factory=list, max_length=3)

    @field_validator("current_topics")
    @classmethod
    def trim_topics(cls, values: list[str]) -> list[str]:
        return [value.strip()[:80] for value in values if value.strip()][:8]


class NargesSelfState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mood: str = Field(default="calm", min_length=2, max_length=40)
    energy: int = Field(default=70, ge=0, le=100)
    activity: str = Field(default="available", min_length=2, max_length=80)
    location: str = Field(default="home", min_length=2, max_length=80)
    is_alone: bool = True
    companions: list[str] = Field(default_factory=list, max_length=4)
    mind_topics: list[str] = Field(default_factory=list, min_length=0, max_length=6)
    note: str | None = Field(default=None, max_length=180)
    updated_at: datetime

    @field_validator("companions", "mind_topics")
    @classmethod
    def trim_short_lists(cls, values: list[str]) -> list[str]:
        return [value.strip()[:80] for value in values if value.strip()]


class NargesSelfStateCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mood: str = Field(min_length=2, max_length=40)
    energy: int = Field(ge=0, le=100)
    activity: str = Field(min_length=2, max_length=80)
    location: str = Field(min_length=2, max_length=80)
    is_alone: bool
    companions: list[str] = Field(default_factory=list, max_length=4)
    mind_topics: list[str] = Field(default_factory=list, max_length=6)
    note: str | None = Field(default=None, max_length=180)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=3, max_length=240)

    @model_validator(mode="after")
    def validate_consistency(self) -> "NargesSelfStateCandidate":
        if self.is_alone and self.companions:
            raise ValueError("state cannot be alone and have companions")
        if not self.is_alone and not self.companions:
            raise ValueError("companions are required when not alone")
        return self


class NargesStateSchedulerSlot(str):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    NIGHT = "night"
