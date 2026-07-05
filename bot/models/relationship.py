from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RelationshipState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    familiarity: int = Field(default=0, ge=0, le=100)
    trust: int = Field(default=0, ge=0, le=100)
    respect: int = Field(default=50, ge=0, le=100)
    comfort: int = Field(default=0, ge=0, le=100)
    joke_permission: bool = False
    nickname: str | None = Field(default=None, max_length=32)
    boundary_warnings: int = Field(default=0, ge=0, le=20)
    intimacy_level: int = Field(default=1, ge=1, le=5)
    current_chat_feeling: str = Field(default="neutral", max_length=40)
    updated_at: datetime
