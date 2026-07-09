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
SUMMARY_TOKEN_THRESHOLD = 900
SHORT_MESSAGE_CHARS = 90
THREAD_LOOKBACK_MESSAGES = 3
SEXUAL_TRIGGER_WORDS = {
    "سکس",
    "سکسی",
    "جنسی",
    "شهوت",
    "تحریک",
    "تحریکم",
    "تحریکت",
    "بوس",
    "بوسم",
    "ببوس",
    "بغل",
    "بدن",
    "لب",
    "گردن",
    "سینه",
    "پستان",
    "ممه",
    "کص",
    "کس",
    "کون",
    "کیر",
    "واژن",
    "آلت",
    "دیک",
    "حشری",
    "هورنی",
    "خیس",
    "لخت",
    "برهنه",
    "نود",
    "پورن",
    "لاس",
    "معاشقه",
    "رابطه جنسی",
    "دخول",
    "ارضا",
    "ارضام",
    "ارضات",
    "بمال",
    "بمالم",
    "بخور",
    "لیس",
    "ساک",
    "بکن",
    "بکنم",
    "میخوامت",
    "می خوامت",
    "sex",
    "sexual",
    "sexy",
    "nsfw",
    "erotic",
    "kiss",
    "horny",
    "nude",
    "naked",
    "fuck",
    "dick",
    "pussy",
    "ass",
    "boob",
    "breast",
    "wet",
    "cum",
    "bj",
    "oral",
}
SEXUAL_BODY_WORDS = {
    "بدن",
    "لب",
    "گردن",
    "سینه",
    "پستان",
    "ممه",
    "کص",
    "کس",
    "کون",
    "کیر",
    "واژن",
    "آلت",
    "دیک",
    "ران",
    "body",
    "lip",
    "neck",
    "boob",
    "breast",
    "pussy",
    "dick",
    "ass",
}
SEXUAL_ACTION_WORDS = {
    "بوس",
    "ببوس",
    "بغل",
    "لمس",
    "بمال",
    "بخور",
    "لیس",
    "ساک",
    "تحریک",
    "ارضا",
    "بکن",
    "بخواب",
    "kiss",
    "touch",
    "lick",
    "fuck",
    "suck",
}
SEXUAL_INTENT_WORDS = {
    "میخوام",
    "میخام",
    "می خوام",
    "می خواهم",
    "دلم میخواد",
    "دلم می خواد",
    "دوست دارم",
    "بیا",
    "بریم",
    "بکنیم",
    "برام",
    "باهام",
    "میخوامت",
    "می خوامت",
    "want",
    "wanna",
    "let's",
    "with me",
}


class ContextBuilder:
    def __init__(self, database: Database, history_service: HistoryService) -> None:
        self.database = database
        self.history_service = history_service

    def build(self, user_id: int, user_text: str, memories: list[MemoryItem]) -> BuiltContext:
        summary = self._summary(user_id)
        row = self._state_row(user_id)
        last_user_messages = self.history_service.recent_user_messages(user_id, limit=THREAD_LOOKBACK_MESSAGES)
        pending_user_thread = self.build_pending_user_thread(user_text, last_user_messages)
        inferred_intent = self.infer_intent(user_text, pending_user_thread)
        mode = "sexual" if self._has_sexual_trigger(user_text) else "normal"
        relationship_stage = row.relationship_stage if row else "new"
        familiarity_score = float(row.familiarity_score or 0) if row else 0.0
        last_assistant = self.history_service.last_assistant_reply(user_id)
        relevant_memories = self._memory_lines(memories)
        return BuiltContext(
            state=ContextState(
                mode=mode,
                topic=self._topic_hint(pending_user_thread or user_text),
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
            current_user_message=user_text,
            last_user_messages=last_user_messages,
            pending_user_thread=pending_user_thread,
            short_conversation_summary=self._short_summary(summary),
            inferred_intent=inferred_intent,
            directly_relevant_memories=relevant_memories,
        )

    def observe_turn(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        assistant_intent: str,
        conversation_state: str = "normal",
        message_datetime: datetime | None = None,
    ) -> None:
        now = (message_datetime or datetime.now(UTC)).astimezone(UTC)
        user_hash = self.history_service.message_hash(user_text)
        assistant_hash = self.history_service.message_hash(assistant_text)
        user_intent = self.infer_intent(user_text)
        mode = "sexual" if self._has_sexual_trigger(user_text) else "normal"
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

    def refresh_summary_with_llm(self, user_id: int, groq_client) -> dict[str, object] | None:
        summary_row = self._summary_row(user_id)
        summarized_id = int(summary_row.summarized_message_id) if summary_row else 0
        existing = summary_row.summary if summary_row else ""
        pending = self.history_service.messages_after(user_id, summarized_id, limit=24)
        if not pending:
            return None
        try:
            if hasattr(groq_client, "complete_conversation_summary_with_usage"):
                summary, usage, provider, model = groq_client.complete_conversation_summary_with_usage(existing, pending)
            else:
                summary = groq_client.complete_conversation_summary(existing, pending)
                usage, provider, model = {}, None, None
        except Exception:
            logger.exception("conversation_summary_refresh_failed user_id=%s", user_id)
            return None
        summary = self._clean_summary(summary)
        if not summary:
            return None
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
        return {
            "usage": usage,
            "provider": provider,
            "model": model,
            "estimated_tokens": sum(estimate_tokens(item["text"]) for item in pending) + estimate_tokens(existing),
            "message_count": len(pending),
            "last_message_id": last_id,
        }

    def infer_intent(self, text: str, pending_user_thread: str = "") -> str:
        lowered = (text or "").lower()
        compact = re.sub(r"\s+", " ", lowered).strip()
        if self._is_guessing(compact):
            return "guessing"
        if pending_user_thread and self._is_continuation(compact):
            return "continuation"
        if any(word in lowered for word in ("bug", "error", "exception", "traceback", "خطا", "باگ")):
            return "technical"
        if any(word in lowered for word in ("code", "api", "sql", "python", "fastapi", "aiogram", "server", "deploy", "render", "database", "postgres", "کد", "سرور", "دیتابیس", "پروژه")):
            return "technical"
        if any(word in lowered for word in ("نه", "اشتباه", "منظورم", "تصحیح", "wrong", "actually")):
            return "correction"
        if any(word in lowered for word in ("خفه", "احمق", "کسکش", "کثافت", "لعنتی", "idiot", "shut up", "stupid")):
            return "insult"
        if any(word in lowered for word in ("ناراحت", "غم", "استرس", "حالم", "تنها", "خسته", "sad", "stress", "lonely")):
            return "support"
        if not compact:
            return "unknown"
        return "casual"

    def build_pending_user_thread(self, current_text: str, last_user_messages: list[dict[str, str]]) -> str:
        messages = [item.get("text", "").strip() for item in last_user_messages if item.get("text")]
        messages.append((current_text or "").strip())
        recent = [message for message in messages[-(THREAD_LOOKBACK_MESSAGES + 1):] if message]
        if not recent or not self._looks_like_pending_thread(recent):
            return ""
        joined = " / ".join(recent)
        lowered = joined.lower()
        topics: list[str] = []
        if any(word in lowered for word in ("project", "پروژه")):
            topics.append("یک پروژه")
        if any(word in lowered for word in ("secret", "confidential", "private", "محرمانه", "خصوصی")):
            topics.append("محرمانه")
        if any(word in lowered for word in ("deploy", "publish", "render", "host", "test", "دیپلوی", "منتشر", "تست", "هاست", "بذارم", "بزارم")):
            topics.append("انتشار، دیپلوی یا تست")
        if topics:
            return f"کاربر در چند پیام کوتاه پشت سر هم درباره {' و '.join(topics)} حرف می‌زند. پیام فعلی ادامه همان thread است."
        return f"کاربر در چند پیام کوتاه پشت سر هم یک منظور ادامه‌دار را می‌سازد: {joined[:260]}"

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
        lines: list[str] = []
        for memory in memories[:8]:
            summary = re.sub(r"\s+", " ", memory.summary).strip()
            created = memory.created_at.astimezone(UTC).date().isoformat()
            updated = memory.updated_at.astimezone(UTC).date().isoformat()
            expires = memory.expires_at.astimezone(UTC).date().isoformat() if memory.expires_at else "none"
            lines.append(f"#{memory.id} | {memory.kind.value} | created_at={created} | updated_at={updated} | expires_at={expires}: {summary[:180]}")
        return lines

    def _conversation_state(self, value: str | None) -> str:
        return value if value in {"normal", "sexual"} else "normal"

    def _has_sexual_trigger(self, text: str) -> bool:
        normalized = self._normalize_trigger_text(text)
        if not normalized:
            return False
        compact = normalized.replace(" ", "")
        tokens = set(re.findall(r"[\wآ-ی]+", normalized, flags=re.UNICODE))
        normalized_triggers = [self._normalize_trigger_text(word) for word in SEXUAL_TRIGGER_WORDS]
        if any(self._contains_trigger(normalized, compact, tokens, word) for word in normalized_triggers):
            return True
        body_words = [self._normalize_trigger_text(word) for word in SEXUAL_BODY_WORDS]
        action_words = [self._normalize_trigger_text(word) for word in SEXUAL_ACTION_WORDS]
        intent_words = [self._normalize_trigger_text(word) for word in SEXUAL_INTENT_WORDS]
        has_body = any(self._contains_trigger(normalized, compact, tokens, word) for word in body_words)
        has_action = any(self._contains_trigger(normalized, compact, tokens, word) for word in action_words)
        has_intent = any(self._contains_trigger(normalized, compact, tokens, word) for word in intent_words)
        return (has_body and has_action) or (has_intent and (has_body or has_action))

    def _contains_trigger(self, normalized: str, compact: str, tokens: set[str], word: str) -> bool:
        if not word:
            return False
        compact_word = word.replace(" ", "")
        if compact_word in {"کس", "کص"}:
            return word in tokens or compact_word in tokens
        if " " in word:
            return word in normalized or compact_word in compact
        return word in normalized or compact_word in compact

    def _normalize_trigger_text(self, text: str) -> str:
        normalized = (text or "").lower()
        normalized = normalized.replace("ي", "ی").replace("ك", "ک").replace("\u200c", "")
        return re.sub(r"\s+", " ", normalized).strip()

    def _mode_for_intent(self, intent: str) -> str:
        if intent == "technical":
            return "technical"
        if intent == "support":
            return "support"
        return "casual"

    def _topic_hint(self, text: str) -> str | None:
        words = re.findall(r"[\w\u0600-\u06FF]{3,}", text or "")
        if not words:
            return None
        return " ".join(words[:8])[:120]

    def _looks_like_pending_thread(self, messages: list[str]) -> bool:
        if len(messages) < 2:
            return False
        short_count = sum(len(message) <= SHORT_MESSAGE_CHARS for message in messages)
        if short_count >= 2 and any(self._is_continuation(message.lower()) or self._is_guessing(message.lower()) for message in messages):
            return True
        joined = " ".join(messages).lower()
        return short_count >= 3 and any(word in joined for word in ("project", "پروژه", "secret", "محرمانه", "deploy", "دیپلوی", "بذارم", "بزارم"))

    def _is_guessing(self, text: str) -> bool:
        normalized = re.sub(r"[؟?!.\s]+", " ", text or "").strip()
        return normalized in {"حدس بزن", "حدس بزنم", "guess", "guess what"} or "حدس بزن" in normalized

    def _is_continuation(self, text: str) -> bool:
        normalized = re.sub(r"[؟?!.\s]+", " ", text or "").strip()
        if len(normalized) <= 2:
            return True
        continuation_values = {
            "خب",
            "باشه",
            "اوکی",
            "اره",
            "آره",
            "نه",
            "نمیگم",
            "هیچی",
            "محرمانه",
            "خصوصی",
            "ادامه",
            "بعدش",
            "یه چیزی",
            "ok",
            "yeah",
            "nope",
            "nothing",
        }
        return normalized in continuation_values or len(normalized) <= 14

    def _next_relationship_stage(self, current: str | None, familiarity_score: float | None) -> str:
        score = float(familiarity_score or 0)
        if score >= 0.75:
            return "close"
        if score >= 0.35:
            return "familiar"
        if score >= 0.1:
            return "warming_up"
        return current or "new"

    def _short_summary(self, summary: str) -> str:
        summary = re.sub(r"\s+", " ", (summary or "").strip())
        return summary[:260]

    def _clean_summary(self, summary: str) -> str:
        summary = re.sub(r"\s+", " ", (summary or "").strip())
        return summary[:700]
