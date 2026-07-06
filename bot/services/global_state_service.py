import json
from datetime import UTC, datetime

from bot.models.state import GlobalState
from bot.storage.database import Database
from bot.storage.orm import GlobalStateORM


class GlobalStateService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self) -> GlobalState:
        with self.database.orm.session() as session:
            row = session.get(GlobalStateORM, 1)
        if row is None:
            state = GlobalState()
            self.save(state)
            return state
        return GlobalState.model_validate(json.loads(row.payload))

    def save(self, state: GlobalState) -> None:
        with self.database.orm.session() as session:
            row = session.get(GlobalStateORM, 1)
            if row is None:
                session.add(GlobalStateORM(id=1, payload=state.model_dump_json(), updated_at=datetime.now(UTC)))
                return
            row.payload = state.model_dump_json()
            row.updated_at = datetime.now(UTC)

    def set_ai_enabled(self, enabled: bool, message: str | None = None) -> GlobalState:
        state = self.get()
        state.ai_enabled = enabled
        if message is not None:
            state.ai_disabled_message = message.strip() or GlobalState().ai_disabled_message
        self.save(state)
        return state
