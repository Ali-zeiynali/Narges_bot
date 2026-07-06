from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select

from bot.models.context import AntiLoopContext, BuiltContext, ContextState
from bot.models.memory import MemoryItem
from bot.services.history_service import HistoryService
from bot.storage.database import Database
from bot.storage.orm import ConversationContextStateORM, ConversationSummaryORM
from bot.utils.tokens import estimate_tokens


logger = logging.getLogger(__name__)


SUMMARY_MESSAGE_THRESHOLD = 8
SUMMARY_TOKEN_THRESHOLD = 1400


class ContextBuilder:
    def __init__(self, database: Database, history_service: HistoryService) -> None:
        self.database = database
        self.history_service = history_service

    def build(self, user_id: int, user_text: str, memories: list[MemoryItem]) -> BuiltContext:
        summary = self._summary(user_id)
        row = self._state_row(user_id)
        inferred_intent = self.infer_intent(user_text)
        mode = self._mode_for_intent(inferred_intent)
        relationship_stage = row.relationship_stage if row else "new"
        familiarity_score = float(row.familiarity_score or 0) if row else 0.0
        last_assistant = self.history_service.last_assistant_reply(user_id)
        relevant_memories = self._memory_lines(memories)
        return BuiltContext(
            state=ContextState(
                mode=mode,
                topic=self._topic_hint(user_text),
                relationship_stage=relationship_stage,
                familiarity_score=familiarity_score,
            ),
            summary=summary,
            facts=[],
            recent_intent=inferred_intent,
            relevant_memories=relevant_memories,
            last_user_message=user_text,
            anti_loop=AntiLoopContext(
                last_assistant_text_hash=last_assistant["text_hash"] if last_assistant else None,
                last_assistant_intent=last_assistant["intent"] if last_assistant else None,
                forbidden_reuse=bool(last_assistant),
            ),
        )

    def observe_turn(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        assistant_intent: str,
        message_datetime: datetime | None = None,
    ) -> None:
        now = (message_datetime or datetime.now(UTC)).astimezone(UTC)
        user_hash = self.history_service.message_hash(user_text)
        assistant_hash = self.history_service.message_hash(assistant_text)
        user_intent = self.infer_intent(user_text)
        mode = self._mode_for_intent(user_intent)
        with self.database.orm.session() as session:
            row = session.get(ConversationContextStateORM, user_id)
            if row is None:
                row = ConversationContextStateORM(user_id=user_id, updated_at=now)
                session.add(row)
            row.mode = mode
            row.topic = self._topic_hint(user_text)
            row.recent_intent = user_intent
            row.intent_confidence = 0.7
            row.relationship_stage = self._next_relationship_stage(row.relationship_stage, row.familiarity_score)
            row.trust_level = min(1.0, float(row.trust_level or 0) + 0.015)
            row.familiarity_score = min(1.0, float(row.familiarity_score or 0) + 0.035)
            row.last_user_message_hash = user_hash
            row.last_assistant_text_hash = assistant_hash
            row.last_assistant_intent = assistant_intent
            row.last_interaction_at = now
            row.updated_at = now

    def should_refresh_summary(self, user_id: int) -> bool:
        summary_row = self._summary_row(user_id)
        summarized_id = int(summary_row.summarized_message_id) if summary_row else 0
        pending_count = self.history_service.count_messages_after(user_id, summarized_id)
        if pending_count >= SUMMARY_MESSAGE_THRESHOLD:
            return True
        pending = self.history_service.messages_after(user_id, summarized_id, limit=SUMMARY_MESSAGE_THRESHOLD)
        pending_tokens = sum(estimate_tokens(item["text"]) for item in pending)
        return pending_tokens >= SUMMARY_TOKEN_THRESHOLD

    def refresh_summary_with_llm(self, user_id: int, groq_client) -> None:
        summary_row = self._summary_row(user_id)
        summarized_id = int(summary_row.summarized_message_id) if summary_row else 0
        existing = summary_row.summary if summary_row else ""
        pending = self.history_service.messages_after(user_id, summarized_id, limit=24)
        if not pending:
            return
        try:
            summary = groq_client.complete_conversation_summary(existing, pending)
        except Exception:
            logger.exception("conversation_summary_refresh_failed user_id=%s", user_id)
            return
        summary = self._clean_summary(summary)
        if not summary:
            return
        last_id = max(int(item["id"]) for item in pending)
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(ConversationSummaryORM, user_id)
            if row is None:
                row = ConversationSummaryORM(user_id=user_id, updated_at=now)
                session.add(row)
            row.summary = summary
            row.summarized_message_id = last_id
            row.message_count = int(row.message_count or 0) + len(pending)
            row.token_estimate = estimate_tokens(summary)
            row.updated_at = now

    def infer_intent(self, text: str) -> str:
        lowered = (text or "").lower()
        if any(word in lowered for word in ("quota", "ظرفیت", "سهمیه", "stars", "استار", "شماره موبایل")):
            return "quota"
        if any(word in lowered for word in ("عضو", "کانال", "membership", "join")):
            return "membership"
        if any(word in lowered for word in ("admin", "ادمین", "provider", "لاگ", "پنل")):
            return "admin_action"
        if any(word in lowered for word in ("bug", "باگ", "خطا", "error", "exception", "traceback")):
            return "bug_report"
        if any(word in lowered for word in ("کد", "api", "sql", "python", "fastapi", "aiogram", "سرور", "مدل")):
            return "technical"
        if any(word in lowered for word in ("ناراحت", "غم", "استرس", "حالم", "تنها")):
            return "support"
        return "casual"

    def _summary(self, user_id: int) -> str:
        row = self._summary_row(user_id)
        return row.summary if row and row.summary else ""

    def _summary_row(self, user_id: int) -> ConversationSummaryORM | None:
        with self.database.orm.session() as session:
            return session.get(ConversationSummaryORM, user_id)

    def _state_row(self, user_id: int) -> ConversationContextStateORM | None:
        with self.database.orm.session() as session:
            return session.get(ConversationContextStateORM, user_id)

    def _memory_lines(self, memories: list[MemoryItem]) -> list[str]:
        return [f"{memory.kind.value}: {memory.summary}" for memory in memories[:10]]

    def _mode_for_intent(self, intent: str) -> str:
        if intent in {"technical", "bug_report"}:
            return "technical"
        if intent == "support":
            return "support"
        if intent == "admin_action":
            return "admin"
        if intent in {"quota", "membership"}:
            return "restricted"
        return "casual"

    def _topic_hint(self, text: str) -> str | None:
        words = re.findall(r"[\w\u0600-\u06FF]{3,}", text or "")
        if not words:
            return None
        return " ".join(words[:8])[:120]

    def _next_relationship_stage(self, current: str | None, familiarity_score: float | None) -> str:
        score = float(familiarity_score or 0)
        if score >= 0.75:
            return "close"
        if score >= 0.35:
            return "familiar"
        if score >= 0.1:
            return "warming_up"
        return current or "new"

    def _clean_summary(self, summary: str) -> str:
        summary = re.sub(r"\s+", " ", (summary or "").strip())
        return summary[:1200]
