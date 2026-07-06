import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy.engine import make_url

from bot.storage.migrations import MIGRATIONS
from bot.storage.orm import DatabaseSessionManager, normalize_database_url


class Database:
    def __init__(self, database_url: str) -> None:
        self.url = normalize_database_url(database_url)
        parsed = make_url(self.url)
        self.path = Path(parsed.database) if parsed.drivername.startswith("sqlite") and parsed.database else None
        if self.path and str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orm = DatabaseSessionManager(self.url)

    @property
    def is_sqlite(self) -> bool:
        return self.orm.is_sqlite

    def connect(self) -> sqlite3.Connection:
        if not self.path:
            raise RuntimeError("Raw sqlite connections are only available for SQLite databases.")
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def migrate(self) -> None:
        if not self.is_sqlite:
            self.orm.ensure_schema()
            return
        with closing(self.connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
            connection.commit()
        self.orm.ensure_schema()

    def execute(self, sql: str, params: Iterable[object] = ()) -> None:
        with closing(self.connect()) as connection:
            connection.execute(sql, tuple(params))
            connection.commit()
