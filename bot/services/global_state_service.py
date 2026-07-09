import json
import threading
from datetime import UTC, datetime, timedelta

from bot.models.state import GlobalState
from bot.storage.database import Database
from bot.storage.orm import GlobalStateORM


class GlobalStateService:
    def __init__(self, database: Database, cache_ttl_seconds: int = 10) -> None:
        self.database = database
        self.cache_ttl = timedelta(seconds=max(1, int(cache_ttl_seconds)))
        self._cache_lock = threading.RLock()
        self._cached_state: GlobalState | None = None
        self._cache_expires_at: datetime | None = None

    def get(self) -> GlobalState:
        cached = self._get_cached()
        if cached is not None:
            return cached
        with self.database.orm.session() as session:
            row = session.get(GlobalStateORM, 1)
        if row is None:
            state = GlobalState()
            self.save(state)
            return state
        state = GlobalState.model_validate(json.loads(row.payload))
        self._set_cached(state)
        return state

    def save(self, state: GlobalState) -> None:
        with self.database.orm.session() as session:
            row = session.get(GlobalStateORM, 1)
            if row is None:
                session.add(GlobalStateORM(id=1, payload=state.model_dump_json(), updated_at=datetime.now(UTC)))
            else:
                row.payload = state.model_dump_json()
                row.updated_at = datetime.now(UTC)
        self._set_cached(state)

    def set_ai_enabled(self, enabled: bool, message: str | None = None) -> GlobalState:
        state = self.get()
        state.ai_enabled = enabled
        if message is not None:
            state.ai_disabled_message = message.strip() or GlobalState().ai_disabled_message
        self.save(state)
        return state

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_state = None
            self._cache_expires_at = None

    def _get_cached(self) -> GlobalState | None:
        with self._cache_lock:
            if self._cached_state is None or self._cache_expires_at is None:
                return None
            if self._cache_expires_at <= datetime.now(UTC):
                self._cached_state = None
                self._cache_expires_at = None
                return None
            return self._cached_state.model_copy(deep=True)

    def _set_cached(self, state: GlobalState) -> None:
        with self._cache_lock:
            self._cached_state = state.model_copy(deep=True)
            self._cache_expires_at = datetime.now(UTC) + self.cache_ttl
