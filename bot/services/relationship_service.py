from contextlib import closing
from datetime import UTC, datetime

from bot.models.ai import RelationshipDelta
from bot.models.relationship import RelationshipState
from bot.storage.database import Database


class RelationshipService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, user_id: int) -> RelationshipState:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM relationships WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            state = RelationshipState(user_id=user_id, updated_at=datetime.now(UTC))
            self.save(state)
            return state
        return RelationshipState(
            user_id=row["user_id"],
            familiarity=row["familiarity"],
            trust=row["trust"],
            respect=row["respect"],
            comfort=row["comfort"],
            joke_permission=bool(row["joke_permission"]),
            nickname=row["nickname"],
            boundary_warnings=row["boundary_warnings"],
            intimacy_level=row["intimacy_level"] if "intimacy_level" in row.keys() else 1,
            current_chat_feeling=row["current_chat_feeling"] if "current_chat_feeling" in row.keys() else "neutral",
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save(self, state: RelationshipState) -> None:
        self.database.execute(
            """
            INSERT INTO relationships(
                user_id, familiarity, trust, respect, comfort,
                joke_permission, nickname, boundary_warnings, intimacy_level,
                current_chat_feeling, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                familiarity = excluded.familiarity,
                trust = excluded.trust,
                respect = excluded.respect,
                comfort = excluded.comfort,
                joke_permission = excluded.joke_permission,
                nickname = excluded.nickname,
                boundary_warnings = excluded.boundary_warnings,
                intimacy_level = excluded.intimacy_level,
                current_chat_feeling = excluded.current_chat_feeling,
                updated_at = excluded.updated_at
            """,
            (
                state.user_id,
                state.familiarity,
                state.trust,
                state.respect,
                state.comfort,
                int(state.joke_permission),
                state.nickname,
                state.boundary_warnings,
                state.intimacy_level,
                state.current_chat_feeling,
                datetime.now(UTC).isoformat(),
            ),
        )

    def apply_delta(self, user_id: int, delta: RelationshipDelta) -> RelationshipState:
        state = self.get(user_id)
        state.familiarity = self._clamp(state.familiarity + delta.familiarity)
        state.trust = self._clamp(state.trust + delta.trust)
        state.respect = self._clamp(state.respect + delta.respect)
        state.comfort = self._clamp(state.comfort + delta.comfort)
        if delta.joke_permission is not None and state.familiarity >= 15:
            state.joke_permission = delta.joke_permission
        if delta.nickname and state.familiarity >= 20:
            state.nickname = delta.nickname.strip()[:32]
        if delta.boundary_warning:
            state.boundary_warnings = min(20, state.boundary_warnings + 1)
        if delta.intimacy_delta:
            state.intimacy_level = max(1, min(5, state.intimacy_level + delta.intimacy_delta))
        if delta.current_chat_feeling:
            state.current_chat_feeling = delta.current_chat_feeling.strip()[:40]
        state.updated_at = datetime.now(UTC)
        self.save(state)
        return state

    def _clamp(self, value: int) -> int:
        return max(0, min(100, value))
