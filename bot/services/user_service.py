from contextlib import closing
from datetime import UTC, datetime

from bot.models.user import OnboardingState, TelegramUserProfile, UserProfile
from bot.storage.database import Database


class UserService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_telegram_user(self, profile: TelegramUserProfile) -> UserProfile:
        now = datetime.now(UTC).isoformat()
        existing = self.get(profile.telegram_id)
        if existing is None:
            self.database.execute(
                """
                INSERT INTO users(
                    telegram_id, username, first_name, last_name, language_code,
                    onboarding_state, plan, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'new', 'free', ?, ?)
                """,
                (
                    profile.telegram_id,
                    profile.username,
                    profile.first_name,
                    profile.last_name,
                    profile.language_code,
                    now,
                    now,
                ),
            )
        else:
            self.database.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?, language_code = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    profile.username,
                    profile.first_name,
                    profile.last_name,
                    profile.language_code,
                    now,
                    profile.telegram_id,
                ),
            )
        return self.get(profile.telegram_id)  # type: ignore[return-value]

    def get(self, user_id: int) -> UserProfile | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return UserProfile(
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            language_code=row["language_code"],
            display_name=row["display_name"],
            suggested_name=row["suggested_name"],
            pending_name=row["pending_name"],
            onboarding_state=OnboardingState(row["onboarding_state"]),
            name_confirm_attempted=bool(row["name_confirm_attempted"]),
            plan=row["plan"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set_state(self, user_id: int, state: OnboardingState) -> None:
        self.database.execute(
            "UPDATE users SET onboarding_state = ?, updated_at = ? WHERE telegram_id = ?",
            (state.value, datetime.now(UTC).isoformat(), user_id),
        )

    def set_suggested_name(self, user_id: int, name: str | None) -> None:
        self.database.execute(
            "UPDATE users SET suggested_name = ?, updated_at = ? WHERE telegram_id = ?",
            (name, datetime.now(UTC).isoformat(), user_id),
        )

    def set_pending_name(self, user_id: int, name: str | None, attempted: bool = True) -> None:
        self.database.execute(
            """
            UPDATE users
            SET pending_name = ?, name_confirm_attempted = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (name, int(attempted), datetime.now(UTC).isoformat(), user_id),
        )

    def save_display_name(self, user_id: int, name: str) -> None:
        self.database.execute(
            """
            UPDATE users
            SET display_name = ?, pending_name = NULL, onboarding_state = 'ready', updated_at = ?
            WHERE telegram_id = ?
            """,
            (name, datetime.now(UTC).isoformat(), user_id),
        )
