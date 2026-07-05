import json
from contextlib import closing
from datetime import UTC, datetime

from bot.models.state import GlobalState
from bot.storage.database import Database


class GlobalStateService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self) -> GlobalState:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT payload FROM global_state WHERE id = 1").fetchone()
        if row is None:
            state = GlobalState()
            self.save(state)
            return state
        return GlobalState.model_validate(json.loads(row["payload"]))

    def save(self, state: GlobalState) -> None:
        self.database.execute(
            """
            INSERT INTO global_state(id, payload, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (state.model_dump_json(), datetime.now(UTC).isoformat()),
        )
