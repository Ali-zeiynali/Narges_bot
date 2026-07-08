import hashlib
import json
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
        provider_response_id: str | None = None,
        safety_metadata: dict | None = None,
        tone_metadata: dict | None = None,
        ai_request_payload: dict | None = None,
        intent: str | None = None,
    ) -> int:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        preview = text[:240]
        now = (created_at or datetime.now(UTC)).astimezone(UTC)
        payload = ai_request_payload or {
            "source": "history_service",
            "role": role,
            "message_type": message_type,
            "chat_id": chat_id,
            "telegram_message_id": telegram_message_id,
            "provider": provider,
            "model": model,
            "stored_at": now.isoformat(),
            "note": "No model request was created for this message; this is the audit payload.",
        }
        with self.database.orm.session() as session:
            session.add(ConversationHistoryORM(user_id=user_id, role=role, text_hash=text_hash, text_preview=preview, created_at=now))
            row = ConversationMessageORM(
                    user_id=user_id,
                    chat_id=chat_id,
                    telegram_message_id=telegram_message_id,
                    role=role,
                    message_type=message_type,
                    text=text,
                    text_hash=text_hash,
                    provider=provider,
                    model=model,
                    provider_response_id=provider_response_id,
                    safety_metadata_json=json.dumps(safety_metadata, ensure_ascii=False, default=str) if safety_metadata else None,
                    tone_metadata_json=json.dumps(tone_metadata, ensure_ascii=False, default=str) if tone_metadata else None,
                    ai_request_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    intent=intent,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    created_at=now,
                )
            session.add(row)
            session.flush()
            return int(row.id)

    def set_telegram_message_id(self, message_id: int | None, telegram_message_id: int | None) -> None:
        if not message_id or not telegram_message_id:
            return
        with self.database.orm.session() as session:
            row = session.get(ConversationMessageORM, int(message_id))
            if row is not None:
                row.telegram_message_id = int(telegram_message_id)

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

    def recent_previous_turns(self, user_id: int, limit: int = 5) -> list[dict[str, str]]:
        return self.recent_user_messages(user_id, limit=limit)

    def recent_user_messages(self, user_id: int, limit: int = 5) -> list[dict[str, str]]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(ConversationMessageORM)
                .where(
                    ConversationMessageORM.user_id == user_id,
                    ConversationMessageORM.message_type == "chat",
                    ConversationMessageORM.role == "user",
                )
                .order_by(ConversationMessageORM.id.desc())
                .limit(limit)
            ).all()
        return [
            {
                "text": self._compact(row.text, 220),
                "created_at": self._iso(row.created_at),
                "intent": row.intent or "",
            }
            for row in reversed(rows)
        ]

    def last_assistant_reply(self, user_id: int) -> dict[str, str] | None:
        with self.database.orm.session() as session:
            row = session.scalar(
                select(ConversationMessageORM)
                .where(
                    ConversationMessageORM.user_id == user_id,
                    ConversationMessageORM.message_type == "chat",
                    ConversationMessageORM.role == "assistant",
                )
                .order_by(ConversationMessageORM.id.desc())
                .limit(1)
            )
        if row is None:
            return None
        return {
            "id": str(row.id),
            "text": row.text,
            "text_hash": row.text_hash,
            "intent": row.intent or "",
            "created_at": self._iso(row.created_at),
        }

    def messages_after(self, user_id: int, message_id: int, limit: int = 20) -> list[dict[str, str]]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(ConversationMessageORM)
                .where(
                    ConversationMessageORM.user_id == user_id,
                    ConversationMessageORM.message_type == "chat",
                    ConversationMessageORM.id > message_id,
                    ConversationMessageORM.role.in_(("user", "assistant")),
                )
                .order_by(ConversationMessageORM.id.asc())
                .limit(limit)
            ).all()
        return [
            {
                "id": str(row.id),
                "role": row.role,
                "text": row.text,
                "created_at": self._iso(row.created_at),
                "intent": row.intent or "",
            }
            for row in rows
        ]

    def count_messages_after(self, user_id: int, message_id: int) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM conversation_messages
                    WHERE user_id = :user_id
                      AND message_type = 'chat'
                      AND id > :message_id
                      AND role IN ('user', 'assistant')
                    """
                ),
                {"user_id": user_id, "message_id": message_id},
            )
        return int(value or 0)

    def message_hash(self, text_value: str) -> str:
        return hashlib.sha256(text_value.encode("utf-8")).hexdigest()

    def search_user_messages(self, user_id: int, query: str, limit: int = 5) -> list[dict[str, str]]:
        query = self._sanitize_fts_query(query)
        if not query:
            return []
        if not self.database.is_sqlite:
            pattern = f"%{query.split()[0]}%"
            with self.database.orm.session() as session:
                rows = session.scalars(
                    select(ConversationMessageORM)
                    .where(
                        ConversationMessageORM.user_id == user_id,
                        ConversationMessageORM.message_type == "chat",
                        ConversationMessageORM.role.in_(("user", "assistant")),
                        ConversationMessageORM.text.ilike(pattern),
                    )
                    .order_by(ConversationMessageORM.id.desc())
                    .limit(limit)
                ).all()
            return [{"text": row.text, "created_at": self._iso(row.created_at)} for row in rows]
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

    def _compact(self, value: str, limit: int) -> str:
        value = " ".join((value or "").split())
        if len(value) <= limit:
            return value
        return value[: limit - 3].rstrip() + "..."

    def _iso(self, value: datetime | str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.isoformat()
        return value
