import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from bot.models.ai import NargesReply, ResponseMode, TelegramOutboundMessage
from bot.persona.compiler import PersonaCompiler
from bot.services.groq_client import GroqChatClient, GroqResult
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.services.conversation_search_tool import ConversationSearchTool
from bot.services.debug_service import DebugService
from bot.services.moderation_service import ModerationService
from bot.services.narges_state_service import NargesStateService
from bot.services.quota_service import QuotaService
from bot.services.style_linter import StyleLinter
from bot.services.usage_service import UsageService
from bot.services.validation import MessageValidator
from bot.utils.tokens import estimate_tokens


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
        user_profile=None,
    ) -> ChatTurnResult:
        message_datetime = (message_datetime or datetime.now(UTC)).astimezone(UTC)
        validation = self.validator.validate(text, "")
        if not validation.ok:
            raise UserFacingError(validation.message)

        state = self.narges_state_service.get_active()
        memories = self.memory_service.retrieve_relevant(user_id, text)
        short_term_messages = self.history_service.recent_turns(user_id, limit=5)
        search_results: list[dict[str, str]] = []

        quota_check = await self.quota_service.begin_generation(user_id)
        if not quota_check.ok:
            raise UserFacingError(quota_check.message)

        input_token_estimate = 0
        assistant_history_message_type = "chat"
        try:
            compiled, messages, input_token_estimate, memories, short_term_messages, search_results = self._compile_under_budget(
                text=text,
                state=state,
                memories=memories,
                short_term_messages=short_term_messages,
                search_results=search_results,
                remaining_quota=quota_check.remaining,
                message_datetime=message_datetime,
                user_profile=user_profile,
            )
            self.debug_service.log(
                "model_request_prepared",
                {
                    "sections": compiled.sections,
                    "message_datetime": message_datetime.isoformat(),
                    "memory_count": len(memories),
                    "short_term_count": len(short_term_messages),
                    "search_result_count": len(search_results),
                    "quota_remaining": quota_check.remaining,
                    "estimated_input_tokens": input_token_estimate,
                },
                user_id=user_id,
            )
            provider_failed = False
            recent_for_lint = self.history_service.recent_assistant_replies(user_id, limit=5)
            result = await self._complete_with_retries(messages)
            result = await self._retry_once_if_needed(result, messages, recent_for_lint)
        except Exception:
            logger.exception("model_response_failed user_id=%s chat_id=%s", user_id, chat_id)
            provider_failed = True
            assistant_history_message_type = "system"
            result = GroqResult(reply=self._fallback_reply(long_user_message=len(text) > 700), raw_text="{}", usage={})

        try:
            if not provider_failed:
                warning = result.reply.warning_suggestion
                if warning and warning.level == "firm":
                    warning_result = self.moderation_service.apply_model_warning(
                        user_id,
                        warning.reason or "security boundary violation",
                        message_id,
                    )
                    result = GroqResult(
                        reply=self._warning_reply(warning_result.message),
                        raw_text="{}",
                        usage=result.usage,
                        provider=result.provider,
                        model=result.model,
                    )
                    provider_failed = True
                    assistant_history_message_type = "warning"
                elif not self.quota_service.can_consume_reply(user_id, result.reply):
                    result = GroqResult(
                        reply=self._quota_fallback_reply(),
                        raw_text="{}",
                        usage=result.usage,
                        provider=result.provider,
                        model=result.model,
                    )
                    provider_failed = True
                    assistant_history_message_type = "system"
                else:
                    self.memory_service.apply_suggestions(user_id, message_id, text, result.reply.memory_suggestions)
                    if result.reply.event_suggestion:
                        logger.info("event_suggestion_ignored_from_chat_model user_id=%s", user_id)
                    self.quota_service.consume_successful_reply(user_id, result.reply)

            assistant_text = "\n".join(message.text for message in result.reply.messages)
            clean_assistant_text = assistant_text
            if self.debug_service.can_debug(user_id):
                result.reply.messages[-1].text += self._format_debug_blocks(
                    result=result,
                    input_token_estimate=input_token_estimate,
                    compiled_sections=compiled.sections,
                    message_datetime=message_datetime,
                )
                assistant_text = "\n".join(message.text for message in result.reply.messages)
            self.history_service.add(
                user_id,
                "user",
                text,
                chat_id=chat_id,
                telegram_message_id=message_id,
                created_at=message_datetime,
                input_tokens=input_token_estimate,
            )
            self.history_service.add(
                user_id,
                "assistant",
                clean_assistant_text,
                chat_id=chat_id,
                provider=result.provider,
                model=result.model,
                message_type=assistant_history_message_type,
                input_tokens=result.usage.get("prompt_tokens") or input_token_estimate,
                output_tokens=result.usage.get("completion_tokens"),
                total_tokens=result.usage.get("total_tokens"),
            )
            self.usage_service.log(
                user_id,
                chat_id,
                input_token_estimate + self.validator.settings.groq_max_completion_tokens,
                result.usage,
                provider=result.provider,
                model=result.model,
            )
        finally:
            await self.quota_service.finish_generation(user_id)

        return ChatTurnResult(
            reply=result.reply,
            usage=result.usage,
            estimated_tokens=input_token_estimate + self.validator.settings.groq_max_completion_tokens,
        )

    def _build_messages(
        self,
        system_prompt: str,
        user_text: str,
        remaining_quota_units: int,
        message_datetime: datetime,
        user_profile=None,
    ) -> list[dict[str, str]]:
        payload = {
            "user_message": user_text,
            "user_profile": self._compact_user_profile(user_profile),
            "remaining_quota_units_today": remaining_quota_units,
        }
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _compile_under_budget(
        self,
        *,
        text: str,
        state,
        memories,
        short_term_messages: list[dict[str, str]],
        search_results: list[dict[str, str]],
        remaining_quota: int,
        message_datetime: datetime,
        user_profile=None,
    ):
        max_input_tokens = min(self.validator.settings.max_api_input_tokens, 3000)
        memories = list(memories)
        short_term_messages = list(short_term_messages)
        search_results = list(search_results)
        history_char_limit = 700

        while True:
            compact_history = self._compact_messages(short_term_messages, history_char_limit)
            compact_search = self._compact_messages(search_results, history_char_limit)
            compiled = self.persona_compiler.compile(
                text,
                state,
                memories,
                None,
                compact_history,
                compact_search,
                message_datetime.isoformat(),
            )
            messages = self._build_messages(compiled.system_prompt, text, remaining_quota, message_datetime, user_profile)
            input_tokens = self._estimate_message_tokens(messages)
            if input_tokens <= max_input_tokens:
                return compiled, messages, input_tokens, memories, compact_history, compact_search
            if search_results:
                search_results.pop(0)
            elif len(memories) > 6:
                memories.pop()
            elif history_char_limit > 180:
                history_char_limit = max(180, history_char_limit // 2)
            elif len(short_term_messages) > 6:
                short_term_messages.pop(0)
            elif memories:
                memories.pop()
            else:
                return compiled, messages, input_tokens, memories, compact_history, compact_search

    def _compact_messages(self, messages: list[dict[str, str]], max_text_chars: int) -> list[dict[str, str]]:
        compacted: list[dict[str, str]] = []
        for item in messages:
            text = str(item.get("text", ""))
            if len(text) > max_text_chars:
                text = text[-max_text_chars:]
            compacted.append({**item, "text": text})
        return [{"text": item["text"], "created_at": item.get("created_at")} for item in compacted]

    def _estimate_message_tokens(self, messages: list[dict[str, str]]) -> int:
        return sum(estimate_tokens(message.get("content", "")) for message in messages)

    def _compact_user_profile(self, profile) -> dict:
        if profile is None:
            return {}
        return {
            "display_name": getattr(profile, "display_name", None),
            "gender": getattr(profile, "gender", None),
            "language_code": getattr(profile, "language_code", None),
        }

    def _format_debug_blocks(
        self,
        *,
        result: GroqResult,
        input_token_estimate: int,
        compiled_sections: tuple[str, ...],
        message_datetime: datetime,
    ) -> str:
        debug_payload = {
            "memory_suggestions": [item.model_dump(mode="json") for item in result.reply.memory_suggestions],
            "warning_suggestion": result.reply.warning_suggestion.model_dump(mode="json") if result.reply.warning_suggestion else None,
            "event_suggestion": result.reply.event_suggestion.model_dump(mode="json") if result.reply.event_suggestion else None,
            "sections": list(compiled_sections),
            "message_datetime": message_datetime.isoformat(),
        }
        usage_payload = {
            "provider": result.provider,
            "model": result.model,
            "estimated_input_tokens": input_token_estimate,
            "provider_usage": result.usage,
        }
        return (
            "\n\n```narges-debug\n"
            + json.dumps(debug_payload, ensure_ascii=False, indent=2, default=str)[:1800]
            + "\n```"
            + "\n\n```token-usage\n"
            + json.dumps(usage_payload, ensure_ascii=False, indent=2, default=str)[:1200]
            + "\n```"
        )

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

    async def _complete_with_retries(self, messages: list[dict[str, str]], attempts: int = 3) -> GroqResult:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncio.to_thread(self.groq_client.complete, messages)
            except Exception as exc:
                last_error = exc
                lowered = str(exc).lower()
                if "429" in lowered or "rate limit" in lowered or "quota" in lowered or "limit" in lowered:
                    break
                if attempt < attempts:
                    await asyncio.sleep(min(2 * attempt, 5))
        raise last_error or RuntimeError("model failed")

    def _fallback_reply(self, long_user_message: bool = False) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SHORT,
            messages=[
                TelegramOutboundMessage(
                    text="یک بار کوتاه‌تر بفرست." if long_user_message else "حالم که بهتر شد دوباره بیا",
                    delay_seconds=0.2,
                )
            ],
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
        )
