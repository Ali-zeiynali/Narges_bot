from datetime import UTC, datetime

from bot.models.ai import RelationshipDelta
from bot.models.relationship import RelationshipState
from bot.storage.database import Database
from bot.storage.orm import RelationshipORM


class RelationshipService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, user_id: int) -> RelationshipState:
        with self.database.orm.session() as session:
            row = session.get(RelationshipORM, user_id)
        if row is None:
            state = RelationshipState(user_id=user_id, updated_at=datetime.now(UTC))
            self.save(state)
            return state
        return RelationshipState(
            user_id=row.user_id,
            familiarity=row.familiarity,
            trust=row.trust,
            respect=row.respect,
            comfort=row.comfort,
            joke_permission=bool(row.joke_permission),
            nickname=row.nickname,
            boundary_warnings=row.boundary_warnings,
            intimacy_level=row.intimacy_level,
            current_chat_feeling=row.current_chat_feeling,
            updated_at=self._as_datetime(row.updated_at),
        )

    def save(self, state: RelationshipState) -> None:
        with self.database.orm.session() as session:
            row = session.get(RelationshipORM, state.user_id)
            if row is None:
                row = RelationshipORM(
                    user_id=state.user_id,
                    familiarity=state.familiarity,
                    trust=state.trust,
                    respect=state.respect,
                    comfort=state.comfort,
                    joke_permission=state.joke_permission,
                    nickname=state.nickname,
                    boundary_warnings=state.boundary_warnings,
                    intimacy_level=state.intimacy_level,
                    current_chat_feeling=state.current_chat_feeling,
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
                return
            row.familiarity = state.familiarity
            row.trust = state.trust
            row.respect = state.respect
            row.comfort = state.comfort
            row.joke_permission = state.joke_permission
            row.nickname = state.nickname
            row.boundary_warnings = state.boundary_warnings
            row.intimacy_level = state.intimacy_level
            row.current_chat_feeling = state.current_chat_feeling
            row.updated_at = datetime.now(UTC)

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

    def _as_datetime(self, value: datetime | str) -> datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)
