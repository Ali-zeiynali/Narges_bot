from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Iterable

from sqlalchemy import func, select, text

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
        normalized_text = text or ""
        now = self._utc(created_at or datetime.now(UTC))
        if role == "user" and chat_id is not None and telegram_message_id is not None:
            existing = self.find_by_telegram_message(chat_id, telegram_message_id, role="user")
            if existing is not None:
                return int(existing["id"])

        row = ConversationMessageORM(
            user_id=user_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            role=role,
            message_type=message_type,
            text=normalized_text,
            text_hash=self.message_hash(normalized_text),
            provider=provider,
            model=model,
            provider_response_id=provider_response_id,
            safety_metadata_json=self._json(safety_metadata),
            tone_metadata_json=self._json(tone_metadata),
            ai_request_payload_json=self._json(ai_request_payload),
            intent=intent,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            created_at=now,
        )
        with self.database.orm.session() as session:
            session.add(row)
            session.flush()
            session.add(
                ConversationHistoryORM(
                    user_id=user_id,
                    role=role,
                    text_hash=row.text_hash,
                    text_preview=self._compact(normalized_text, 1000),
                    created_at=now,
                )
            )
            return int(row.id)

    def find_by_telegram_message(self, chat_id: int, telegram_message_id: int, role: str | None = None) -> dict[str, str] | None:
        query = select(ConversationMessageORM).where(
            ConversationMessageORM.chat_id == chat_id,
            ConversationMessageORM.telegram_message_id == telegram_message_id,
        )
        if role:
            query = query.where(ConversationMessageORM.role == role)
        with self.database.orm.session() as session:
            row = session.scalar(query.order_by(ConversationMessageORM.id.desc()).limit(1))
        return self._row_dict(row) if row is not None else None

    def set_telegram_message_id(self, message_id: int | None, telegram_message_id: int | None) -> None:
        if not message_id or not telegram_message_id:
            return
        with self.database.orm.session() as session:
            row = session.get(ConversationMessageORM, int(message_id))
            if row is not None:
                row.telegram_message_id = int(telegram_message_id)

    def recent_assistant_replies(
        self,
        user_id: int,
        limit: int = 5,
        *,
        chat_id: int | None = None,
        message_types: Iterable[str] = ("chat", "admin_direct"),
    ) -> list[str]:
        query = select(ConversationMessageORM.text).where(
            ConversationMessageORM.user_id == user_id,
            ConversationMessageORM.role == "assistant",
            ConversationMessageORM.message_type.in_(tuple(message_types)),
        )
        if chat_id is not None:
            query = query.where(ConversationMessageORM.chat_id == chat_id)
        with self.database.orm.session() as session:
            rows = session.scalars(query.order_by(ConversationMessageORM.id.desc()).limit(limit)).all()
        return [self._compact(value, 420) for value in rows if value]

    def recent_turns(
        self,
        user_id: int,
        limit: int = 6,
        *,
        chat_id: int | None = None,
        message_type: str = "chat",
    ) -> list[dict[str, str]]:
        query = select(ConversationMessageORM).where(
            ConversationMessageORM.user_id == user_id,
            ConversationMessageORM.message_type == message_type,
            ConversationMessageORM.role.in_(("user", "assistant")),
        )
        if chat_id is not None:
            query = query.where(ConversationMessageORM.chat_id == chat_id)
        with self.database.orm.session() as session:
            rows = session.scalars(query.order_by(ConversationMessageORM.id.desc()).limit(limit)).all()
        return [
            {
                "role": row.role,
                "text": self._compact(row.text, 500),
                "created_at": self._iso(row.created_at),
            }
            for row in reversed(rows)
        ]

    def recent_previous_turns(self, user_id: int, limit: int = 5) -> list[dict[str, str]]:
        return self.recent_user_messages(user_id, limit=limit)

    def recent_user_messages(
        self,
        user_id: int,
        limit: int = 5,
        *,
        chat_id: int | None = None,
        message_type: str = "chat",
    ) -> list[dict[str, str]]:
        query = select(ConversationMessageORM).where(
            ConversationMessageORM.user_id == user_id,
            ConversationMessageORM.message_type == message_type,
            ConversationMessageORM.role == "user",
        )
        if chat_id is not None:
            query = query.where(ConversationMessageORM.chat_id == chat_id)
        with self.database.orm.session() as session:
            rows = session.scalars(query.order_by(ConversationMessageORM.id.desc()).limit(limit)).all()
        return [
            {
                "id": str(row.id),
                "text": self._compact(row.text, 260),
                "created_at": self._iso(row.created_at),
                "intent": row.intent or "",
            }
            for row in reversed(rows)
        ]

    def last_assistant_reply(
        self,
        user_id: int,
        *,
        chat_id: int | None = None,
        message_types: Iterable[str] = ("chat", "admin_direct"),
    ) -> dict[str, str] | None:
        query = select(ConversationMessageORM).where(
            ConversationMessageORM.user_id == user_id,
            ConversationMessageORM.message_type.in_(tuple(message_types)),
            ConversationMessageORM.role == "assistant",
        )
        if chat_id is not None:
            query = query.where(ConversationMessageORM.chat_id == chat_id)
        with self.database.orm.session() as session:
            row = session.scalar(query.order_by(ConversationMessageORM.id.desc()).limit(1))
        return self._row_dict(row) if row is not None else None

    def messages_after(
        self,
        user_id: int,
        message_id: int,
        limit: int = 20,
        *,
        chat_id: int | None = None,
    ) -> list[dict[str, str]]:
        query = select(ConversationMessageORM).where(
            ConversationMessageORM.user_id == user_id,
            ConversationMessageORM.message_type == "chat",
            ConversationMessageORM.id > message_id,
            ConversationMessageORM.role.in_(("user", "assistant")),
        )
        if chat_id is not None:
            query = query.where(ConversationMessageORM.chat_id == chat_id)
        with self.database.orm.session() as session:
            rows = session.scalars(query.order_by(ConversationMessageORM.id.asc()).limit(limit)).all()
        return [
            {
                "id": str(row.id),
                "role": row.role,
                "text": self._compact(row.text, 800),
                "created_at": self._iso(row.created_at),
                "intent": row.intent or "",
            }
            for row in rows
        ]

    def count_messages_after(self, user_id: int, message_id: int, *, chat_id: int | None = None) -> int:
        query = select(func.count()).select_from(ConversationMessageORM).where(
            ConversationMessageORM.user_id == user_id,
            ConversationMessageORM.message_type == "chat",
            ConversationMessageORM.id > message_id,
            ConversationMessageORM.role.in_(("user", "assistant")),
        )
        if chat_id is not None:
            query = query.where(ConversationMessageORM.chat_id == chat_id)
        with self.database.orm.session() as session:
            value = session.scalar(query)
        return int(value or 0)

    def message_hash(self, text_value: str) -> str:
        normalized = "\n".join(line.rstrip() for line in (text_value or "").strip().splitlines())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def search_user_messages(
        self,
        user_id: int,
        query: str,
        limit: int = 5,
        *,
        chat_id: int | None = None,
        roles: Iterable[str] = ("user", "assistant"),
    ) -> list[dict[str, str]]:
        sanitized = self._sanitize_fts_query(query)
        if not sanitized:
            return []
        role_values = tuple(role for role in roles if role in {"user", "assistant"}) or ("user", "assistant")
        if not self.database.is_sqlite:
            words = self._query_words(query)
            pattern = f"%{words[0]}%" if words else f"%{query[:40]}%"
            statement = select(ConversationMessageORM).where(
                ConversationMessageORM.user_id == user_id,
                ConversationMessageORM.message_type == "chat",
                ConversationMessageORM.role.in_(role_values),
                ConversationMessageORM.text.ilike(pattern),
            )
            if chat_id is not None:
                statement = statement.where(ConversationMessageORM.chat_id == chat_id)
            with self.database.orm.session() as session:
                rows = session.scalars(statement.order_by(ConversationMessageORM.id.desc()).limit(limit)).all()
            return [self._search_row(row) for row in rows]

        role_sql = ",".join(f"'{role}'" for role in role_values)
        chat_clause = "AND cm.chat_id = :chat_id" if chat_id is not None else ""
        statement = text(
            f"""
            SELECT cm.id, cm.role, cm.text, cm.created_at, cm.intent
            FROM conversation_messages_fts fts
            JOIN conversation_messages cm ON cm.id = fts.rowid
            WHERE conversation_messages_fts MATCH :query
              AND cm.user_id = :user_id
              AND cm.message_type = 'chat'
              AND cm.role IN ({role_sql})
              {chat_clause}
            ORDER BY bm25(conversation_messages_fts), cm.id DESC
            LIMIT :limit
            """
        )
        params: dict[str, object] = {"query": sanitized, "user_id": user_id, "limit": limit}
        if chat_id is not None:
            params["chat_id"] = chat_id
        with self.database.orm.session() as session:
            rows = session.execute(statement, params).mappings().all()
        return [
            {
                "id": str(row["id"]),
                "role": str(row["role"]),
                "text": self._compact(str(row["text"]), 420),
                "created_at": self._iso(row["created_at"]),
                "intent": str(row["intent"] or ""),
            }
            for row in rows
        ]

    def _search_row(self, row) -> dict[str, str]:
        return {
            "id": str(row.id),
            "role": row.role,
            "text": self._compact(row.text, 420),
            "created_at": self._iso(row.created_at),
            "intent": row.intent or "",
        }

    def _row_dict(self, row) -> dict[str, str]:
        return {
            "id": str(row.id),
            "role": row.role,
            "text": row.text,
            "text_hash": row.text_hash,
            "intent": row.intent or "",
            "created_at": self._iso(row.created_at),
            "message_type": row.message_type,
        }

    def _sanitize_fts_query(self, query: str) -> str:
        words = self._query_words(query)
        return " OR ".join(f'"{word}"' for word in words[:8])

    def _query_words(self, query: str) -> list[str]:
        normalized = (query or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
        words = re.findall(r"[\w\u0600-\u06FF]{2,}", normalized.lower())
        stop = {"این", "اون", "برای", "چرا", "چی", "what", "the", "and", "with"}
        return [word for word in words if word not in stop]

    def _compact(self, value: str, limit: int) -> str:
        compact = re.sub(r"\s+", " ", (value or "").strip())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"

    def _json(self, value: dict | None) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def _iso(self, value: datetime | str) -> str:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return self._utc(parsed).isoformat()

    def _utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
