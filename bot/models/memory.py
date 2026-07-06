from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(str, Enum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    PROJECT = "project"
    GOAL = "goal"
    CONSTRAINT = "constraint"
    INSIDE_JOKE = "inside_joke"
    BOUNDARY = "boundary"
    UNRESOLVED_TOPIC = "unresolved_topic"
    TEMPORARY_EVENT = "temporary_event"


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    user_id: int
    kind: MemoryKind
    summary: str = Field(min_length=3, max_length=240)
    confidence: float = Field(ge=0, le=1)
    importance: int = Field(default=3, ge=1, le=5)
    source_message_id: int | None = None
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None = None
    expires_at: datetime | None = None
    active: bool = True
