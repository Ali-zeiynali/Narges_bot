import hashlib
from datetime import UTC, datetime

from sqlalchemy import select, text

from bot.storage.database import Database
from bot.storage.orm import ConversationHistoryORM, ConversationMessageORM


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
        created_at: datetime | None = None,
        message_type: str = "chat",
        provider: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        preview = text[:240]
        now = (created_at or datetime.now(UTC)).astimezone(UTC)
        with self.database.orm.session() as session:
            session.add(ConversationHistoryORM(user_id=user_id, role=role, text_hash=text_hash, text_preview=preview, created_at=now))
            session.add(
                ConversationMessageORM(
                    user_id=user_id,
                    chat_id=chat_id,
                    telegram_message_id=telegram_message_id,
                    role=role,
                    message_type=message_type,
                    text=text,
                    text_hash=text_hash,
                    provider=provider,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    created_at=now,
                )
            )

    def recent_assistant_replies(self, user_id: int, limit: int = 5) -> list[str]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(ConversationHistoryORM.text_preview)
                .where(ConversationHistoryORM.user_id == user_id, ConversationHistoryORM.role == "assistant")
                .order_by(ConversationHistoryORM.id.desc())
                .limit(limit)
            ).all()
        return list(rows)

    def recent_turns(self, user_id: int, limit: int = 5) -> list[dict[str, str]]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(ConversationMessageORM)
                .where(
                    ConversationMessageORM.user_id == user_id,
                    ConversationMessageORM.message_type == "chat",
                    ConversationMessageORM.role.in_(("user", "assistant")),
                )
                .order_by(ConversationMessageORM.id.desc())
                .limit(limit)
            ).all()
        return [
            {
                "role": row.role,
                "text": row.text,
                "created_at": self._iso(row.created_at),
            }
            for row in reversed(rows)
        ]

    def search_user_messages(self, user_id: int, query: str, limit: int = 5) -> list[dict[str, str]]:
        query = self._sanitize_fts_query(query)
        if not query:
            return []
        with self.database.orm.session() as session:
            rows = session.execute(
                text(
                    """
                SELECT cm.text, cm.created_at
                FROM conversation_messages_fts fts
                JOIN conversation_messages cm ON cm.id = fts.rowid
                WHERE conversation_messages_fts MATCH :query
                  AND cm.user_id = :user_id
                  AND cm.message_type = 'chat'
                  AND cm.role IN ('user', 'assistant')
                ORDER BY bm25(conversation_messages_fts)
                LIMIT :limit
                """,
                ),
                {"query": query, "user_id": user_id, "limit": limit},
            ).mappings().all()
        return [{"text": row["text"], "created_at": row["created_at"]} for row in rows]

    def _sanitize_fts_query(self, query: str) -> str:
        words = [word.strip('"*():-') for word in (query or "").split()]
        words = [word for word in words if len(word) >= 2]
        return " OR ".join(words[:8])

    def _iso(self, value: datetime | str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.isoformat()
        return value
