from datetime import UTC, datetime

from bot.models.user import OnboardingState, TelegramUserProfile, UserProfile
from bot.storage.database import Database
from bot.storage.orm import UserORM


class UserService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_telegram_user(self, profile: TelegramUserProfile) -> UserProfile:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(UserORM, profile.telegram_id)
            if row is None:
                row = UserORM(
                    telegram_id=profile.telegram_id,
                    username=profile.username,
                    first_name=profile.first_name,
                    last_name=profile.last_name,
                    language_code=profile.language_code,
                    onboarding_state=OnboardingState.NEW.value,
                    plan="free",
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.username = profile.username
                row.first_name = profile.first_name
                row.last_name = profile.last_name
                row.language_code = profile.language_code
                row.updated_at = now
        return self.get(profile.telegram_id)  # type: ignore[return-value]

    def get(self, user_id: int) -> UserProfile | None:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            return self._to_profile(row) if row else None

    def set_state(self, user_id: int, state: OnboardingState) -> None:
        self._update(user_id, onboarding_state=state.value)

    def set_suggested_name(self, user_id: int, name: str | None) -> None:
        self._update(user_id, suggested_name=name)

    def set_pending_name(self, user_id: int, name: str | None, attempted: bool = True) -> None:
        self._update(user_id, pending_name=name, name_confirm_attempted=attempted)

    def save_display_name(self, user_id: int, name: str) -> None:
        self._update(
            user_id,
            display_name=name,
            pending_name=None,
            suggested_name=None,
            onboarding_state=OnboardingState.ASK_GENDER.value,
        )

    def save_gender(self, user_id: int, gender: str | None) -> None:
        self._update(
            user_id,
            gender=gender,
            onboarding_state=OnboardingState.READY.value,
        )

    def save_phone_number(self, user_id: int, phone_number: str) -> bool:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None:
                return False
            row.phone_number = phone_number
            row.phone_verified_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            return not row.phone_bonus_claimed

    def mark_phone_bonus_claimed(self, user_id: int) -> None:
        self._update(user_id, phone_bonus_claimed=True)

    def _update(self, user_id: int, **values) -> None:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)

    def _to_profile(self, row: UserORM) -> UserProfile:
        return UserProfile(
            telegram_id=row.telegram_id,
            username=row.username,
            first_name=row.first_name,
            last_name=row.last_name,
            language_code=row.language_code,
            display_name=row.display_name,
            gender=row.gender,
            suggested_name=row.suggested_name,
            pending_name=row.pending_name,
            onboarding_state=OnboardingState(row.onboarding_state),
            name_confirm_attempted=bool(row.name_confirm_attempted),
            plan=row.plan,
            phone_number=row.phone_number,
            phone_verified_at=row.phone_verified_at,
            phone_bonus_claimed=bool(row.phone_bonus_claimed),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
