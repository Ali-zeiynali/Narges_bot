import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from bot.models.ai import NargesReply, RelationshipDelta, ResponseMode, TelegramOutboundMessage
from bot.persona.compiler import PersonaCompiler
from bot.persona.texts.engine_prompts import CHAT_QUOTA_COST_RULE, CHAT_USER_PAYLOAD_INSTRUCTION
from bot.services.groq_client import GroqChatClient, GroqResult
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.services.conversation_search_tool import ConversationSearchTool
from bot.services.debug_service import DebugService
from bot.services.moderation_service import ModerationService
from bot.services.relationship_service import RelationshipService
from bot.services.narges_state_service import NargesStateService
from bot.services.quota_service import QuotaService
from bot.services.style_linter import StyleLinter
from bot.services.usage_service import UsageService
from bot.services.validation import MessageValidator


logger = logging.getLogger(__name__)


class UserFacingError(Exception):
    pass


@dataclass(frozen=True)
class ChatTurnResult:
    reply: NargesReply
    usage: dict[str, int | None]
    estimated_tokens: int


class ChatService:
    def __init__(
        self,
        validator: MessageValidator,
        persona_compiler: PersonaCompiler,
        groq_client: GroqChatClient,
        narges_state_service: NargesStateService,
        memory_service: MemoryService,
        relationship_service: RelationshipService,
        history_service: HistoryService,
        conversation_search_tool: ConversationSearchTool,
        moderation_service: ModerationService,
        debug_service: DebugService,
        usage_service: UsageService,
        style_linter: StyleLinter,
        quota_service: QuotaService,
    ) -> None:
        self.validator = validator
        self.persona_compiler = persona_compiler
        self.groq_client = groq_client
        self.narges_state_service = narges_state_service
        self.memory_service = memory_service
        self.relationship_service = relationship_service
        self.history_service = history_service
        self.conversation_search_tool = conversation_search_tool
        self.moderation_service = moderation_service
        self.debug_service = debug_service
        self.usage_service = usage_service
        self.style_linter = style_linter
        self.quota_service = quota_service

    async def answer(
        self,
        user_id: int,
        chat_id: int,
        message_id: int | None,
        text: str,
        message_datetime: datetime | None = None,
    ) -> ChatTurnResult:
        message_datetime = (message_datetime or datetime.now(UTC)).astimezone(UTC)
        state = self.narges_state_service.get_active()
        relationship = self.relationship_service.get(user_id)
        memories = self.memory_service.retrieve_relevant(user_id, text)
        short_term_messages = self.history_service.recent_turns(user_id, limit=10)
        search_results = self._maybe_search_history(user_id, text)
        recent = self.history_service.recent_assistant_replies(user_id)
        compiled = self.persona_compiler.compile(
            text,
            state,
            relationship,
            memories,
            recent,
            short_term_messages,
            search_results,
            message_datetime.isoformat(),
        )

        validation = self.validator.validate(text, compiled.system_prompt)
        if not validation.ok:
            raise UserFacingError(validation.message)

        quota_check = await self.quota_service.begin_generation(user_id)
        if not quota_check.ok:
            raise UserFacingError(quota_check.message)

        messages = self._build_messages(compiled.system_prompt, text, quota_check.remaining, message_datetime)
        self.debug_service.log(
            "model_request_prepared",
            {
                "sections": compiled.sections,
                "message_datetime": message_datetime.isoformat(),
                "memory_count": len(memories),
                "short_term_count": len(short_term_messages),
                "search_result_count": len(search_results),
                "quota_remaining": quota_check.remaining,
            },
            user_id=user_id,
        )
        provider_failed = False
        try:
            result = await asyncio.to_thread(self.groq_client.complete, messages)
            result = await self._retry_once_if_needed(result, messages, recent)
        except Exception:
            logger.exception("model_response_failed user_id=%s chat_id=%s", user_id, chat_id)
            provider_failed = True
            result = GroqResult(reply=self._fallback_reply(), raw_text="{}", usage={})

        try:
            if not provider_failed:
                warning = result.reply.warning_suggestion
                if warning and warning.level == "firm":
                    warning_result = self.moderation_service.apply_model_warning(
                        user_id,
                        warning.reason or "security boundary violation",
                        message_id,
                    )
                    self.relationship_service.apply_delta(user_id, result.reply.relationship_delta)
                    result = GroqResult(reply=self._warning_reply(warning_result.message), raw_text="{}", usage=result.usage)
                    provider_failed = True
                elif not self.quota_service.can_consume_reply(user_id, result.reply):
                    result = GroqResult(reply=self._quota_fallback_reply(), raw_text="{}", usage=result.usage)
                    provider_failed = True
                else:
                    self.memory_service.apply_suggestions(user_id, message_id, text, result.reply.memory_suggestions)
                    self.relationship_service.apply_delta(user_id, result.reply.relationship_delta)
                    if result.reply.event_suggestion:
                        logger.info("event_suggestion_ignored_from_chat_model user_id=%s", user_id)
                    self.quota_service.consume_successful_reply(user_id, result.reply)
                    self.debug_service.log(
                        "model_response_accepted",
                        {
                            "mode": result.reply.mode.value,
                            "messages": [message.text for message in result.reply.messages],
                            "memory_suggestions": [item.model_dump() for item in result.reply.memory_suggestions],
                            "relationship_delta": result.reply.relationship_delta.model_dump(),
                            "usage": result.usage,
                        },
                        user_id=user_id,
                    )

            assistant_text = "\n".join(message.text for message in result.reply.messages)
            if self.debug_service.can_debug(user_id):
                result.reply.messages[-1].text += self.debug_service.format_block(
                    {
                        "sections": compiled.sections,
                        "message_datetime": message_datetime.isoformat(),
                        "quota": self.quota_service.account_quota(user_id),
                        "usage": result.usage,
                        "memories": [memory.model_dump(mode="json") for memory in memories],
                        "short_term": short_term_messages,
                        "search_results": search_results,
                    }
                )
                assistant_text = "\n".join(message.text for message in result.reply.messages)
            self.history_service.add(
                user_id,
                "user",
                text,
                chat_id=chat_id,
                telegram_message_id=message_id,
                created_at=message_datetime,
            )
            self.history_service.add(user_id, "assistant", assistant_text, chat_id=chat_id)
            self.usage_service.log(user_id, chat_id, validation.estimated_tokens, result.usage)
        finally:
            await self.quota_service.finish_generation(user_id)

        return ChatTurnResult(
            reply=result.reply,
            usage=result.usage,
            estimated_tokens=validation.estimated_tokens,
        )

    def _build_messages(
        self,
        system_prompt: str,
        user_text: str,
        remaining_quota_units: int,
        message_datetime: datetime,
    ) -> list[dict[str, str]]:
        payload = {
            "user_message": user_text,
            "current_message_datetime": message_datetime.isoformat(),
            "instruction": CHAT_USER_PAYLOAD_INSTRUCTION,
            "remaining_quota_units_today": remaining_quota_units,
            "quota_cost_rule": CHAT_QUOTA_COST_RULE,
        }
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _maybe_search_history(self, user_id: int, text: str) -> list[dict[str, str]]:
        lowered = text.lower()
        triggers = ["remember", "previous", "گفتیم", "یادته", "قبلاً", "قبلن", "جستجو"]
        if not any(trigger in lowered for trigger in triggers):
            return []
        return self.conversation_search_tool.search(user_id, text, limit=5)

    async def _retry_once_if_needed(
        self,
        result: GroqResult,
        messages: list[dict[str, str]],
        recent: list[str],
    ) -> GroqResult:
        texts = [message.text for message in result.reply.messages]
        lint = self.style_linter.lint(texts, recent)
        if not lint.serious:
            return result

        retry_messages = messages + [
            {
                "role": "user",
                "content": (
                    "پاسخ قبلی مشکل سبک داشت: "
                    f"{lint.feedback}. فقط یک JSON معتبر کوتاه‌تر و طبیعی‌تر بده."
                ),
            }
        ]
        try:
            return await asyncio.to_thread(self.groq_client.complete, retry_messages)
        except Exception:
            logger.exception("model_retry_failed")
            return result

    def _fallback_reply(self) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SHORT,
            messages=[
                TelegramOutboundMessage(
                    text="الان جوابم درست آماده نشد. یک بار کوتاه‌تر بفرست.",
                    delay_seconds=0.2,
                )
            ],
            relationship_delta=RelationshipDelta(),
        )

    def _quota_fallback_reply(self) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SHORT,
            messages=[
                TelegramOutboundMessage(
                    text="باقی‌مانده امروزت برای این جواب کافی نیست. یک پیام کوتاه‌تر بفرست یا فردا دوباره امتحان کن.",
                    delay_seconds=0.2,
                )
            ],
            relationship_delta=RelationshipDelta(),
        )

    def _warning_reply(self, text: str) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SERIOUS,
            messages=[
                TelegramOutboundMessage(
                    text=text,
                    delay_seconds=0.1,
                )
            ],
            relationship_delta=RelationshipDelta(),
        )
