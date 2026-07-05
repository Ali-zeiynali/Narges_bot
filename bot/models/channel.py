from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RequiredChannel:
    id: int
    chat_id: str
    title: str
    join_url: str | None
    position: int
    is_private: bool
    active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MembershipItem:
    channel: RequiredChannel
    status: str | None
    is_member: bool
    error: str | None = None


@dataclass(frozen=True)
class MembershipCheck:
    ok: bool
    missing: list[MembershipItem]
    errors: list[MembershipItem]
    bypassed: bool = False
