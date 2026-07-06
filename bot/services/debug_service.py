import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select

from bot.config import Settings
from bot.storage.database import Database
from bot.storage.orm import DebugLogORM


logger = logging.getLogger(__name__)


class DebugService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.debug_users = set(settings.debug_user_ids) | set(settings.admin_ids)

    @property
    def enabled(self) -> bool:
        return self.settings.debug_mode

    def can_debug(self, user_id: int) -> bool:
        return self.enabled and user_id in self.debug_users

    def log(self, event: str, payload: dict[str, Any], user_id: int | None = None) -> None:
        if not self.enabled:
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)
        logger.info("debug_event event=%s user_id=%s payload=%s", event, user_id, text)
        with self.database.orm.session() as session:
            session.add(
                DebugLogORM(
                    user_id=user_id,
                    event=event,
                    payload=text,
                    created_at=datetime.now(UTC),
                )
            )

    def recent(self, limit: int = 20, user_id: int | None = None) -> list[dict[str, Any]]:
        with self.database.orm.session() as session:
            statement = select(DebugLogORM).order_by(desc(DebugLogORM.id)).limit(max(1, min(limit, 100)))
            if user_id is not None:
                statement = statement.where(DebugLogORM.user_id == user_id)
            rows = session.scalars(statement).all()
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "event": row.event,
                "payload": row.payload,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    def format_block(self, payload: dict[str, Any]) -> str:
        if not self.enabled:
            return ""
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return f"\n\n```debug\n{text[:3500]}\n```"
