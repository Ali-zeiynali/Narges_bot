import secrets
from datetime import UTC, datetime

from bot.models.user import OnboardingState, TelegramUserProfile, UserProfile
from bot.storage.database import Database
from bot.storage.orm import UserORM
from sqlalchemy import select


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
                    onboarding_state=OnboardingState.NOT_STARTED.value,
                    registration_state=OnboardingState.NOT_STARTED.value,
                    referral_code=self._new_referral_code(profile.telegram_id),
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

    def user_ids(self) -> list[int]:
        with self.database.orm.session() as session:
            rows = session.scalars(select(UserORM.telegram_id).order_by(UserORM.updated_at.desc())).all()
        return [int(row) for row in rows]

    def set_state(self, user_id: int, state: OnboardingState) -> None:
        self._update(user_id, onboarding_state=state.value, registration_state=state.value)

    def set_onboarding_state(self, user_id: int, state: OnboardingState) -> None:
        self._update(user_id, onboarding_state=state.value)

    def mark_membership_required(self, user_id: int) -> None:
        self._update(user_id, membership_state="required")

    def mark_membership_ok(self, user_id: int) -> None:
        self._update(user_id, membership_state="ok", last_membership_gate_chat_id=None, last_membership_gate_message_id=None)

    def set_membership_gate_message(self, user_id: int, chat_id: int, message_id: int) -> None:
        self._update(user_id, last_membership_gate_chat_id=chat_id, last_membership_gate_message_id=message_id)

    def clear_membership_gate_message(self, user_id: int) -> None:
        self._update(user_id, last_membership_gate_chat_id=None, last_membership_gate_message_id=None)

    def set_prompt_message(self, user_id: int, chat_id: int, message_id: int) -> None:
        self._update(user_id, last_prompt_chat_id=chat_id, last_prompt_message_id=message_id)

    def clear_prompt_message(self, user_id: int) -> None:
        self._update(user_id, last_prompt_chat_id=None, last_prompt_message_id=None)

    def recover_registration_state(self, user_id: int) -> UserProfile | None:
        profile = self.get(user_id)
        if profile is None:
            return None
        if profile.onboarding_state != OnboardingState.NEED_CHANNELS:
            return profile
        if profile.display_name and profile.gender:
            self.set_state(user_id, OnboardingState.READY)
        elif profile.display_name:
            self.set_state(user_id, OnboardingState.ASK_GENDER)
        else:
            self.set_state(user_id, OnboardingState.NOT_STARTED)
        return self.get(user_id)

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
            registration_state=OnboardingState.ASK_GENDER.value,
        )

    def save_display_name_keep_state(self, user_id: int, name: str) -> None:
        self._update(user_id, display_name=name, pending_name=None, suggested_name=None)

    def save_gender(self, user_id: int, gender: str | None) -> None:
        self._update(
            user_id,
            gender=gender,
            onboarding_state=OnboardingState.READY.value,
            registration_state=OnboardingState.READY.value,
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

    def set_referred_by_code(self, user_id: int, referral_code: str | None) -> None:
        if not referral_code:
            return
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            inviter = session.scalar(select(UserORM).where(UserORM.referral_code == referral_code))
            if row is None or inviter is None or inviter.telegram_id == user_id or row.referred_by_user_id:
                return
            row.referred_by_user_id = inviter.telegram_id
            row.updated_at = datetime.now(UTC)

    def mark_first_question(self, user_id: int) -> bool:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None:
                return False
            if row.first_question_at is None:
                row.first_question_at = datetime.now(UTC)
                row.updated_at = datetime.now(UTC)
            return self._is_referral_qualified(row)

    def should_send_gender_nudge(self, user_id: int, today: str) -> bool:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None or row.gender or row.last_gender_nudge_date == today:
                return False
            row.last_gender_nudge_date = today
            row.updated_at = datetime.now(UTC)
            return True

    def mark_reengagement_sent(self, user_id: int) -> None:
        self._update(user_id, last_reengagement_sent_at=datetime.now(UTC))

    def claim_referral_bonus_if_ready(self, user_id: int) -> int | None:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None or not self._is_referral_qualified(row) or row.referral_bonus_claimed_at is not None:
                return None
            row.referral_bonus_claimed_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
            return int(row.referred_by_user_id) if row.referred_by_user_id else None

    def referral_stats(self, user_id: int) -> dict:
        with self.database.orm.session() as session:
            rows = session.scalars(select(UserORM).where(UserORM.referred_by_user_id == user_id).order_by(UserORM.created_at.desc())).all()
        return {
            "code": self.ensure_referral_code(user_id),
            "total": len(rows),
            "qualified": sum(1 for row in rows if self._is_referral_qualified(row)),
            "rewarded": sum(1 for row in rows if row.referral_bonus_claimed_at is not None),
            "users": rows,
        }

    def ensure_referral_code(self, user_id: int) -> str:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None:
                return self._new_referral_code(user_id)
            if not row.referral_code:
                row.referral_code = self._new_referral_code(user_id)
                row.updated_at = datetime.now(UTC)
            return row.referral_code

    def _update(self, user_id: int, **values) -> None:
        with self.database.orm.session() as session:
            row = session.get(UserORM, user_id)
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)

    def _to_profile(self, row: UserORM) -> UserProfile:
        onboarding_state = OnboardingState(row.onboarding_state)
        if onboarding_state == OnboardingState.NEED_CHANNELS:
            if row.display_name and row.gender:
                onboarding_state = OnboardingState.READY
            elif row.display_name:
                onboarding_state = OnboardingState.ASK_GENDER
            else:
                onboarding_state = OnboardingState.NOT_STARTED
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
            onboarding_state=onboarding_state,
            registration_state=OnboardingState(row.registration_state or onboarding_state.value),
            membership_state=row.membership_state or "unknown",
            last_membership_gate_chat_id=row.last_membership_gate_chat_id,
            last_membership_gate_message_id=row.last_membership_gate_message_id,
            last_prompt_chat_id=row.last_prompt_chat_id,
            last_prompt_message_id=row.last_prompt_message_id,
            last_gender_nudge_date=row.last_gender_nudge_date,
            last_reengagement_sent_at=row.last_reengagement_sent_at,
            referral_code=row.referral_code,
            referred_by_user_id=row.referred_by_user_id,
            first_question_at=row.first_question_at,
            referral_bonus_claimed_at=row.referral_bonus_claimed_at,
            name_confirm_attempted=bool(row.name_confirm_attempted),
            plan=row.plan,
            phone_number=row.phone_number,
            phone_verified_at=row.phone_verified_at,
            phone_bonus_claimed=bool(row.phone_bonus_claimed),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _new_referral_code(self, user_id: int) -> str:
        return f"n{user_id:x}{secrets.token_hex(3)}"

    def _is_referral_qualified(self, row: UserORM) -> bool:
        return bool(row.referred_by_user_id and row.onboarding_state == OnboardingState.READY.value and row.first_question_at)
