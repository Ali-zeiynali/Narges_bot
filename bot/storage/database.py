import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import text
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
            self._ensure_runtime_schema()
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
        self._ensure_runtime_schema()

    def execute(self, sql: str, params: Iterable[object] = ()) -> None:
        with closing(self.connect()) as connection:
            connection.execute(sql, tuple(params))
            connection.commit()

    def _ensure_runtime_schema(self) -> None:
        if self.orm.dialect_name == "postgresql":
            statements = [
                "ALTER TABLE media_files ADD COLUMN IF NOT EXISTS content_hash VARCHAR(128)",
                "ALTER TABLE media_files ADD COLUMN IF NOT EXISTS file_bytes BYTEA",
                "ALTER TABLE group_chats ADD COLUMN IF NOT EXISTS member_count INTEGER",
                "CREATE INDEX IF NOT EXISTS idx_media_files_content_hash ON media_files(content_hash)",
            ]
            with self.orm.engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
        elif self.orm.dialect_name == "sqlite":
            with closing(self.connect()) as connection:
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(media_files)").fetchall()}
                if "content_hash" not in columns:
                    connection.execute("ALTER TABLE media_files ADD COLUMN content_hash TEXT")
                if "file_bytes" not in columns:
                    connection.execute("ALTER TABLE media_files ADD COLUMN file_bytes BLOB")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_media_files_content_hash ON media_files(content_hash)")
                group_columns = {row["name"] for row in connection.execute("PRAGMA table_info(group_chats)").fetchall()}
                if group_columns and "member_count" not in group_columns:
                    connection.execute("ALTER TABLE group_chats ADD COLUMN member_count INTEGER")
                connection.commit()
