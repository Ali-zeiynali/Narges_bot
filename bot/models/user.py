from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OnboardingState(str, Enum):
    NEW = "new"
    NEED_CHANNELS = "need_channels"
    ASK_NAME_CONFIRM = "ask_name_confirm"
    ASK_NAME_INPUT = "ask_name_input"
    NAME_AMBIGUOUS_CONFIRM = "name_ambiguous_confirm"
    ASK_GENDER = "ask_gender"
    READY = "ready"


@dataclass(frozen=True)
class TelegramUserProfile:
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    language_code: str | None


@dataclass(frozen=True)
class UserProfile:
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    language_code: str | None
    display_name: str | None
    gender: str | None
    suggested_name: str | None
    pending_name: str | None
    onboarding_state: OnboardingState
    name_confirm_attempted: bool
    plan: str
    phone_number: str | None
    phone_verified_at: datetime | None
    phone_bonus_claimed: bool
    created_at: datetime
    updated_at: datetime
