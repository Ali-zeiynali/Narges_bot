import hashlib
from contextlib import closing
from datetime import UTC, datetime

from bot.storage.database import Database


class HistoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(
        self,
        user_id: int,
        role: str,
        text: str,
        chat_id: int | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        preview = text[:240]
        now = datetime.now(UTC).isoformat()
        self.database.execute(
            """
            INSERT INTO conversation_history(user_id, role, text_hash, text_preview, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, role, text_hash, preview, now),
        )
        self.database.execute(
            """
            INSERT INTO conversation_messages(
                user_id, chat_id, telegram_message_id, role, text, text_hash, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, chat_id, telegram_message_id, role, text, text_hash, now),
        )

    def recent_assistant_replies(self, user_id: int, limit: int = 5) -> list[str]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT text_preview FROM conversation_history
                WHERE user_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [row["text_preview"] for row in rows]

    def recent_turns(self, user_id: int, limit: int = 10) -> list[dict[str, str]]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT role, text, created_at FROM conversation_messages
                WHERE user_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [
            {"role": row["role"], "text": row["text"], "created_at": row["created_at"]}
            for row in reversed(rows)
        ]

    def search_user_messages(self, user_id: int, query: str, limit: int = 5) -> list[dict[str, str]]:
        query = self._sanitize_fts_query(query)
        if not query:
            return []
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT cm.role, cm.text, cm.created_at
                FROM conversation_messages_fts fts
                JOIN conversation_messages cm ON cm.id = fts.rowid
                WHERE conversation_messages_fts MATCH ? AND cm.user_id = ?
                ORDER BY bm25(conversation_messages_fts)
                LIMIT ?
                """,
                (query, user_id, limit),
            ).fetchall()
        return [{"role": row["role"], "text": row["text"], "created_at": row["created_at"]} for row in rows]

    def _sanitize_fts_query(self, query: str) -> str:
        words = [word.strip('"*():-') for word in (query or "").split()]
        words = [word for word in words if len(word) >= 2]
        return " OR ".join(words[:8])
