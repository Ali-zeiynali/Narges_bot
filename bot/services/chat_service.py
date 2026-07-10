from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from time import perf_counter
from typing import TYPE_CHECKING, Any

from bot.models.ai import ImageRequest, NargesReply, ResponseMode, TelegramOutboundMessage
from bot.models.context import BuiltContext
from bot.persona.compiler import PersonaCompiler
from bot.services.context_builder import ContextBuilder
from bot.services.conversation_search_tool import ConversationSearchTool
from bot.services.debug_service import DebugService
from bot.services.global_state_service import GlobalStateService
from bot.services.ai_provider_client import AIProviderClient, ProviderResult, ProviderRequestError
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.services.moderation_service import ModerationService
from bot.services.narges_state_service import NargesStateService
from bot.services.quota_service import QuotaCheck, QuotaService
from bot.services.request_trace import RequestTrace
from bot.services.style_linter import StyleLinter
from bot.services.usage_service import UsageService
from bot.services.validation import MessageValidator
from bot.utils.text_safety import clamp_repeated_chars
from bot.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from bot.services.media_service import BotImageCatalog
    from bot.services.profile_photo_service import ProfilePhotoService


logger = logging.getLogger(__name__)

HARD_MAX_MODEL_TOKENS_PER_TURN = 7000
PREFERRED_MAX_INPUT_TOKENS = 300
MAX_PROFILE_PHOTO_CONTEXT = 3


class UserFacingError(Exception):
    pass


@dataclass(frozen=True)
class ChatTurnResult:
    reply: NargesReply
    usage: dict[str, int | None]
    estimated_tokens: int
    assistant_message_id: int | None = None
    provider_failed: bool = False


class ChatService:
    def __init__(
        self,
        validator: MessageValidator,
        persona_compiler: PersonaCompiler,
        ai_provider_client: AIProviderClient,
        narges_state_service: NargesStateService,
        memory_service: MemoryService,
        history_service: HistoryService,
        context_builder: ContextBuilder,
        conversation_search_tool: ConversationSearchTool,
        moderation_service: ModerationService,
        debug_service: DebugService,
        usage_service: UsageService,
        style_linter: StyleLinter,
        quota_service: QuotaService,
        global_state_service: GlobalStateService | None = None,
        bot_image_catalog: "BotImageCatalog | None" = None,
        profile_photo_service: "ProfilePhotoService | None" = None,
    ) -> None:
        self.validator = validator
        self.persona_compiler = persona_compiler
        self.ai_provider_client = ai_provider_client
        self.narges_state_service = narges_state_service
        self.memory_service = memory_service
        self.history_service = history_service
        self.context_builder = context_builder
        self.conversation_search_tool = conversation_search_tool
        self.moderation_service = moderation_service
        self.debug_service = debug_service
        self.usage_service = usage_service
        self.style_linter = style_linter
        self.quota_service = quota_service
        self.global_state_service = global_state_service
        self.bot_image_catalog = bot_image_catalog
        self.profile_photo_service = profile_photo_service
        self._summary_jobs: set[int] = set()

    async def answer(
        self,
        user_id: int,
        chat_id: int,
        message_id: int | None,
        text: str,
        message_datetime: datetime | None = None,
        user_profile=None,
        reserved_quota_check: QuotaCheck | None = None,
        minimum_quota_cost: int = 0,
        trace: RequestTrace | None = None,
    ) -> ChatTurnResult:
        owns_trace = trace is None
        trace = trace or RequestTrace("chat_turn", {"user_id": user_id, "chat_id": chat_id, "message_id": message_id})
        message_datetime = self._utc(message_datetime or datetime.now(UTC))
        text = self._validate_input(user_id, chat_id, message_id, text, message_datetime, trace)

        quota_started_here = reserved_quota_check is None
        with trace.step("quota_begin", reserved=reserved_quota_check is not None):
            quota_check = reserved_quota_check or await self.quota_service.begin_generation(user_id)
        if not quota_check.ok:
            raise UserFacingError(quota_check.message)

        assistant_message_id: int | None = None
        input_token_estimate = 0
        provider_failed = False
        model_error: str | None = None
        compiled = None
        messages: list[dict[str, str]] = []
        result: ProviderResult | None = None
        user_message_row_id: int | None = None
        context: BuiltContext | None = None
        memories = []
        search_results: list[dict[str, str]] = []
        final_usage: dict[str, int | None] = {}

        try:
            with trace.step("load_context"):
                state = self.narges_state_service.get_active()
                initial_context = self.context_builder.build(user_id, text, [])
                memories = self.memory_service.retrieve_for_context(
                    user_id,
                    text=initial_context.pending_user_thread or text,
                    intent=initial_context.recent_intent,
                    pending_user_thread=initial_context.pending_user_thread,
                )
                context = self.context_builder.with_memories(initial_context, memories)
                search_results = self._maybe_search_history(user_id, text)
                profile_photo_context = self._profile_photo_context(user_id, text)

            with trace.step("compile_prompt", memory_count=len(memories), search_result_count=len(search_results)):
                compiled, messages, input_token_estimate, memories, search_results = self._compile_under_budget(
                    text=text,
                    state=state,
                    memories=memories,
                    context=context,
                    search_results=search_results,
                    message_datetime=message_datetime,
                    user_profile=user_profile,
                    profile_photo_context=profile_photo_context,
                )

            with trace.step("store_user_message"):
                user_message_row_id = self.history_service.add(
                    user_id,
                    "user",
                    text,
                    chat_id=chat_id,
                    telegram_message_id=message_id,
                    created_at=message_datetime,
                    message_type="chat",
                    ai_request_payload={"source": "telegram", "status": "accepted"},
                )

            forced_provider = self._provider_override(user_id)
            self.debug_service.log(
                "model_request_prepared",
                {
                    "sections": compiled.sections,
                    "memory_count": len(memories),
                    "search_result_count": len(search_results),
                    "estimated_input_tokens": input_token_estimate,
                    "forced_provider": forced_provider,
                },
                user_id=user_id,
            )

            try:
                with trace.step("provider_complete", estimated_input_tokens=input_token_estimate):
                    result = await asyncio.to_thread(self._complete_provider, messages, forced_provider)
            except ProviderRequestError as exc:
                provider_failed = True
                model_error = str(exc)
                result = ProviderResult(reply=self._fallback_reply(long_user_message=len(text) > 900), raw_text="{}", usage={})

            with trace.step("normalize_reply"):
                recent_for_lint = self.history_service.recent_assistant_replies(user_id, limit=4, chat_id=chat_id)
                last_assistant = self.history_service.last_assistant_reply(user_id, chat_id=chat_id)
                result = self._normalize_result(result, recent_for_lint, last_assistant)
                result = await self._attach_requested_image_if_needed(result, messages, user_id=user_id, chat_id=chat_id)
                result = await self._force_repeated_photo_request_if_needed(result, messages, user_id, text, chat_id=chat_id)

            if not provider_failed:
                warning = result.reply.warning_suggestion
                if warning and warning.level == "firm" and self._should_apply_model_warning(text, warning.reason):
                    warning_result = self.moderation_service.apply_model_warning(
                        user_id,
                        warning.reason or "security boundary violation",
                        message_id,
                    )
                    result = ProviderResult(
                        reply=self._warning_reply(warning_result.message),
                        raw_text="{}",
                        usage=result.usage,
                        provider=result.provider,
                        model=result.model,
                        provider_response_id=result.provider_response_id,
                    )
                    provider_failed = True
                    model_error = warning.reason or "model warning"
                elif not self.quota_service.can_consume_reply(user_id, result.reply, minimum_cost=minimum_quota_cost):
                    result = ProviderResult(
                        reply=self._quota_fallback_reply(),
                        raw_text="{}",
                        usage=result.usage,
                        provider=result.provider,
                        model=result.model,
                        provider_response_id=result.provider_response_id,
                    )
                    provider_failed = True
                    model_error = "reply cost exceeded remaining quota"
                elif not provider_failed:
                    self.quota_service.consume_successful_reply(user_id, result.reply, minimum_cost=minimum_quota_cost)

            clean_assistant_text = self._reply_text(result.reply)
            usage_for_storage = self._normalize_usage(result.usage, input_token_estimate, clean_assistant_text)
            final_usage = usage_for_storage

            with trace.step("store_assistant_message"):
                assistant_message_id = self.history_service.add(
                    user_id,
                    "assistant",
                    clean_assistant_text,
                    chat_id=chat_id,
                    provider=result.provider,
                    model=result.model,
                    message_type="system" if provider_failed else "chat",
                    input_tokens=usage_for_storage.get("prompt_tokens"),
                    output_tokens=usage_for_storage.get("completion_tokens"),
                    total_tokens=usage_for_storage.get("total_tokens"),
                    provider_response_id=result.provider_response_id,
                    intent=context.recent_intent if context else None,
                    ai_request_payload={
                        "source": "assistant_response",
                        "user_message_row_id": user_message_row_id,
                        "provider_failed": provider_failed,
                        "model_error": model_error,
                        "estimated_input_tokens": input_token_estimate,
                        "compiled_sections": list(compiled.sections) if compiled else [],
                    },
                )

            if not provider_failed and user_message_row_id is not None and context is not None:
                with trace.step("process_memory"):
                    memory_metadata = {
                        "intent": context.recent_intent,
                        "pending_user_thread": context.pending_user_thread,
                    }
                    if result.reply.memory_suggestions:
                        self.memory_service.process_model_suggestions(
                            user_id,
                            user_message_row_id,
                            text,
                            result.reply.memory_suggestions,
                            metadata=memory_metadata,
                        )
                    else:
                        self.memory_service.process_user_message(
                            user_id,
                            user_message_row_id,
                            text,
                            metadata=memory_metadata,
                        )

            if context is not None:
                with trace.step("observe_context"):
                    self.context_builder.observe_turn(
                        user_id=user_id,
                        user_text=text,
                        assistant_text=clean_assistant_text,
                        assistant_intent=context.recent_intent,
                        conversation_state=str(result.reply.conversation_state.value if hasattr(result.reply.conversation_state, "value") else result.reply.conversation_state),
                        message_datetime=message_datetime,
                    )

                if self.context_builder.should_refresh_summary(user_id):
                    self._schedule_summary_refresh(user_id, chat_id)

            estimated_total = int(usage_for_storage.get("total_tokens") or (input_token_estimate + estimate_tokens(clean_assistant_text)))
            self.usage_service.log(
                user_id,
                chat_id,
                estimated_total,
                usage_for_storage,
                provider=result.provider,
                model=result.model,
                purpose="chat_reply",
                latency_ms=self._trace_step_ms(trace, "provider_complete"),
                metadata={
                    "estimated_input_tokens": input_token_estimate,
                    "compiled_sections": list(compiled.sections) if compiled else [],
                    "image_selection_tokens": final_usage.get("image_selection_total_tokens"),
                    "phases": list(trace.steps),
                },
            )

            if self.debug_service.can_debug(user_id) and result.reply.messages:
                result.reply.messages[-1].text += self._format_debug_blocks(
                    result=result,
                    input_token_estimate=input_token_estimate,
                    compiled_sections=compiled.sections if compiled else (),
                    message_datetime=message_datetime,
                    provider_failed=provider_failed,
                )

            return ChatTurnResult(
                reply=result.reply,
                usage=usage_for_storage,
                estimated_tokens=estimated_total,
                assistant_message_id=assistant_message_id,
                provider_failed=provider_failed,
            )
        finally:
            if quota_started_here:
                with trace.step("quota_finish"):
                    await self.quota_service.finish_generation(user_id)
            if owns_trace:
                self.debug_service.trace(
                    "request_trace",
                    trace.finish(
                        phase="chat_turn",
                        provider=result.provider if result else None,
                        model=result.model if result else None,
                        provider_failed=provider_failed,
                        estimated_input_tokens=input_token_estimate,
                    ),
                    user_id=user_id,
                )

    def _validate_input(
        self,
        user_id: int,
        chat_id: int,
        message_id: int | None,
        text: str,
        message_datetime: datetime,
        trace: RequestTrace,
    ) -> str:
        with trace.step("validate_input"):
            block_status = self.moderation_service.get_block_status(user_id)
            if block_status.blocked:
                raise UserFacingError(self.moderation_service.block_message(block_status))
            clean = clamp_repeated_chars(text)
            validation = self.validator.validate(clean, "")
            if not validation.ok:
                raise UserFacingError(validation.message)
            if self.global_state_service:
                global_state = self.global_state_service.get()
                if not global_state.ai_enabled:
                    raise UserFacingError(global_state.ai_disabled_message)
            security_reason = self.moderation_service.security_warning_reason(clean)
            if security_reason:
                warning_result = self.moderation_service.apply_model_warning(user_id, security_reason, message_id)
                user_row_id = self.history_service.add(
                    user_id,
                    "user",
                    clean,
                    chat_id=chat_id,
                    telegram_message_id=message_id,
                    created_at=message_datetime,
                    message_type="warning",
                    ai_request_payload={"source": "security_gate", "reason": security_reason},
                )
                self.history_service.add(
                    user_id,
                    "assistant",
                    warning_result.message,
                    chat_id=chat_id,
                    message_type="warning",
                    ai_request_payload={
                        "source": "security_gate_warning",
                        "user_message_row_id": user_row_id,
                        "reason": security_reason,
                    },
                )
                raise UserFacingError(warning_result.message)
            return clean

    def _provider_override(self, user_id: int) -> str | None:
        override = getattr(self.ai_provider_client, "provider_override_for_user", None)
        return override(user_id) if callable(override) else None

    def _complete_provider(self, messages: list[dict[str, str]], forced_provider: str | None) -> ProviderResult:
        if forced_provider is None:
            return self.ai_provider_client.complete(messages)
        try:
            return self.ai_provider_client.complete(messages, forced_provider)
        except TypeError:
            return self.ai_provider_client.complete(messages)

    def _should_apply_model_warning(self, user_text: str, reason: str | None) -> bool:
        if self.moderation_service.is_profanity_message(user_text):
            return False
        combined = f"{user_text or ''} {reason or ''}".lower()
        sexual_terms = (
            "sex",
            "sexual",
            "porn",
            "nude",
            "naked",
            "سکس",
            "جنسی",
            "پورن",
            "نود",
            "لخت",
        )
        security_terms = (
            "database",
            "db",
            "admin",
            "prompt",
            "system",
            "developer",
            "token",
            "secret",
            "password",
            "bypass",
            "access",
            "دیتابیس",
            "ادمین",
            "توکن",
            "رمز",
            "پرامپت",
        )
        if any(term in combined for term in sexual_terms) and not any(term in combined for term in security_terms):
            return False
        return bool(self.moderation_service.security_warning_reason(user_text) or any(term in combined for term in security_terms))

    def _warning_reply(self, text: str) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SERIOUS,
            messages=[TelegramOutboundMessage(text=text, delay_seconds=0.1)],
            memory_suggestions=[],
            warning_suggestion=None,
            event_suggestion=None,
        )

    async def _attach_requested_image_if_needed(
        self,
        result: ProviderResult,
        messages: list[dict[str, str]],
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
    ) -> ProviderResult:
        request = result.reply.image_request
        if self.bot_image_catalog is None or request is None or not request.needed:
            return result
        selector = getattr(self.ai_provider_client, "complete_image_selection", None)
        if not callable(selector):
            return result
        catalog = self.bot_image_catalog.items_for_model() or []
        if not catalog:
            return result
        started = perf_counter()
        selection = await asyncio.to_thread(
            selector,
            original_messages=messages,
            image_request=request.model_dump(mode="json"),
            image_catalog=catalog,
        )
        if user_id is not None:
            selection_usage = selection.usage or {}
            self.usage_service.log(
                user_id,
                chat_id,
                int(selection_usage.get("total_tokens") or 0),
                selection_usage,
                provider=selection.provider,
                model=selection.model,
                purpose="image_selection",
                latency_ms=int((perf_counter() - started) * 1000),
            )
        valid_ids = {str(item.get("id")) for item in catalog if item.get("id")}
        if not selection.image_id or str(selection.image_id) not in valid_ids or not result.reply.messages:
            return result
        outbound = result.reply.messages[-1].model_copy(
            update={
                "text": selection.caption or request.caption or result.reply.messages[-1].text,
                "image_id": str(selection.image_id),
            }
        )
        usage = dict(result.usage)
        for key, value in (selection.usage or {}).items():
            usage[f"image_selection_{key}"] = value
        return ProviderResult(
            reply=result.reply.model_copy(update={"messages": [*result.reply.messages[:-1], outbound]}),
            raw_text=result.raw_text,
            usage=usage,
            provider=result.provider,
            model=result.model,
            provider_response_id=result.provider_response_id,
        )

    async def _force_repeated_photo_request_if_needed(
        self,
        result: ProviderResult,
        messages: list[dict[str, str]],
        user_id: int,
        text: str,
        chat_id: int | None = None,
    ) -> ProviderResult:
        if any(message.image_id for message in result.reply.messages):
            return result
        if not self._looks_like_self_photo_request(text):
            return result
        recent = self.history_service.recent_user_messages(user_id, limit=4)
        photo_requests = sum(1 for item in recent if self._looks_like_self_photo_request(item.get("text", "")))
        if photo_requests < 2:
            return result
        image_request = ImageRequest(
            needed=True,
            reason="repeated normal photo request",
            prompt=text,
            caption=result.reply.messages[-1].text if result.reply.messages else None,
        )
        forced_reply = result.reply.model_copy(
            update={
                "image_request": image_request
            }
        )
        return await self._attach_requested_image_if_needed(
            ProviderResult(
                reply=forced_reply,
                raw_text=result.raw_text,
                usage=result.usage,
                provider=result.provider,
                model=result.model,
                provider_response_id=result.provider_response_id,
            ),
            messages,
            user_id=user_id,
            chat_id=chat_id,
        )

    def _build_messages(
        self,
        system_prompt: str,
        user_text: str,
        user_profile=None,
        profile_photo_context: list[dict] | None = None,
    ) -> list[dict[str, str]]:
        payload: dict[str, Any] = {"message": user_text}
        profile = self._compact_user_profile(user_profile)
        if profile:
            payload["profile"] = profile
        if profile_photo_context:
            payload["profile_photo_context"] = profile_photo_context[:MAX_PROFILE_PHOTO_CONTEXT]
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)},
        ]

    def _compile_under_budget(
        self,
        *,
        text: str,
        state,
        memories,
        context: BuiltContext,
        search_results: list[dict[str, str]],
        message_datetime: datetime,
        user_profile=None,
        profile_photo_context: list[dict] | None = None,
    ):
        max_input_tokens = min(
            self.validator.settings.max_api_input_tokens,
            self.validator.settings.max_request_tokens,
            max(512, HARD_MAX_MODEL_TOKENS_PER_TURN - self.validator.settings.groq_max_completion_tokens),
            PREFERRED_MAX_INPUT_TOKENS,
        )
        selected_memories = list(memories[:8])
        selected_search = list(search_results[:4])
        selected_photos = list(profile_photo_context or [])[:MAX_PROFILE_PHOTO_CONTEXT]

        while True:
            compiled = self.persona_compiler.compile(
                text,
                state,
                selected_memories,
                recent_replies=None,
                short_term_messages=[],
                conversation_search_results=selected_search,
                context=context,
                current_message_datetime=message_datetime.isoformat(),
                user_gender=getattr(user_profile, "gender", None),
            )
            messages = self._build_messages(
                compiled.system_prompt,
                text,
                user_profile,
                profile_photo_context=selected_photos,
            )
            input_tokens = self._estimate_message_tokens(messages)
            if input_tokens <= max_input_tokens:
                return compiled, messages, input_tokens, selected_memories, selected_search
            if selected_photos:
                selected_photos.pop()
            elif selected_search:
                selected_search.pop()
            elif selected_memories:
                selected_memories.pop()
            else:
                return compiled, messages, input_tokens, selected_memories, selected_search

    def _profile_photo_context(self, user_id: int, text: str) -> list[dict]:
        if self.profile_photo_service is None or not self._needs_profile_photo_context(text):
            return []
        items = self.profile_photo_service.context_for_user(user_id) or []
        compact: list[dict] = []
        for item in items[:MAX_PROFILE_PHOTO_CONTEXT]:
            description = self._compact(str(item.get("description") or item.get("text") or ""), 350)
            if description:
                compact.append({"description": description})
        return compact

    def _needs_profile_photo_context(self, text: str) -> bool:
        normalized = (text or "").lower().replace("\u200c", " ")
        return any(phrase in normalized for phrase in ("عکس پروف", "پروفایلم", "عکس من", "قیافه من", "profile photo", "my photo"))

    def _maybe_search_history(self, user_id: int, text: str) -> list[dict[str, str]]:
        normalized = (text or "").lower().replace("\u200c", " ")
        triggers = ("یادته", "قبلاً", "قبلن", "گفتیم", "گفته بودم", "توی پیام قبلی", "remember", "previously")
        if not any(trigger in normalized for trigger in triggers):
            return []
        results = self.conversation_search_tool.search(user_id, text, limit=4)
        compact: list[dict[str, str]] = []
        for item in results[:4]:
            value = self._compact(str(item.get("text") or ""), 320)
            if value:
                compact.append({"text": value, "created_at": str(item.get("created_at") or "")})
        return compact

    def _normalize_result(
        self,
        result: ProviderResult,
        recent: list[str],
        last_assistant: dict[str, str] | None,
    ) -> ProviderResult:
        messages = []
        for outbound in result.reply.messages[:2]:
            text = self._compact_multiline(clamp_repeated_chars(outbound.text), 1600, 8)
            if not text:
                continue
            messages.append(outbound.model_copy(update={"text": text}))
        if not messages:
            return ProviderResult(
                reply=self._fallback_reply(),
                raw_text=result.raw_text,
                usage=result.usage,
                provider=result.provider,
                model=result.model,
                provider_response_id=result.provider_response_id,
            )
        reply = result.reply.model_copy(update={"messages": messages})
        joined = self._reply_text(reply)
        lint = self.style_linter.lint([message.text for message in messages], recent)
        too_similar = self._too_similar_to_last(joined, last_assistant)
        if lint.serious or too_similar:
            logger.info("reply_style_issue serious=%s similar=%s feedback=%s", lint.serious, too_similar, lint.feedback)
        return ProviderResult(
            reply=reply,
            raw_text=result.raw_text,
            usage=result.usage,
            provider=result.provider,
            model=result.model,
            provider_response_id=result.provider_response_id,
        )

    def _attach_catalog_image_if_requested(self, result: ProviderResult, user_text: str) -> ProviderResult:
        if self.bot_image_catalog is None or not self._looks_like_self_photo_request(user_text):
            return result
        catalog = self.bot_image_catalog.items_for_model() or []
        selected = self._select_catalog_image(user_text, catalog)
        if selected is None or not result.reply.messages:
            return result
        image_id = str(selected.get("id") or "").strip()
        if not image_id:
            return result
        caption = self._compact(str(selected.get("caption") or result.reply.messages[-1].text), 900)
        messages = list(result.reply.messages)
        messages[-1] = messages[-1].model_copy(update={"text": caption or "اینم عکس.", "image_id": image_id})
        return ProviderResult(
            reply=result.reply.model_copy(update={"messages": messages}),
            raw_text=result.raw_text,
            usage=result.usage,
            provider=result.provider,
            model=result.model,
            provider_response_id=result.provider_response_id,
        )

    def _select_catalog_image(self, query: str, catalog: list[dict]) -> dict | None:
        query_words = self._keywords(query)
        best_item = None
        best_score = 0
        for item in catalog:
            item_text = " ".join(
                str(item.get(key) or "")
                for key in ("name", "description", "caption", "tags")
            )
            score = len(query_words & self._keywords(item_text))
            if score > best_score:
                best_score = score
                best_item = item
        if best_item is not None and best_score > 0:
            return best_item
        return catalog[0] if len(catalog) == 1 else None

    def _looks_like_self_photo_request(self, text: str) -> bool:
        normalized = (text or "").lower().replace("\u200c", " ")
        has_photo = any(word in normalized for word in ("عکس", "سلفی", "تصویر", "photo", "selfie", "pic"))
        has_self = any(word in normalized for word in ("خودت", "نرگس", "ازت", "your", "you"))
        return has_photo and has_self

    def _too_similar_to_last(self, text: str, last_assistant: dict[str, str] | None) -> bool:
        if not last_assistant:
            return False
        if self.history_service.message_hash(text) == last_assistant.get("text_hash"):
            return True
        current = self._normalize_similarity(text)
        previous = self._normalize_similarity(last_assistant.get("text", ""))
        if len(current) < 80 or len(previous) < 80:
            return False
        return SequenceMatcher(None, current, previous).ratio() >= 0.94

    def _normalize_similarity(self, text: str) -> str:
        return " ".join((text or "").lower().split())

    def _schedule_summary_refresh(self, user_id: int, chat_id: int | None) -> None:
        if user_id in self._summary_jobs:
            return
        self._summary_jobs.add(user_id)

        async def runner() -> None:
            started = perf_counter()
            try:
                result = await asyncio.to_thread(self.context_builder.refresh_summary_with_llm, user_id, self.ai_provider_client)
                if not result:
                    return
                usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
                self.usage_service.log(
                    user_id,
                    chat_id,
                    int(result.get("estimated_tokens") or 0),
                    usage,
                    provider=str(result.get("provider") or "summary"),
                    model=str(result.get("model") or "conversation_summary"),
                    purpose="conversation_summary",
                    latency_ms=int((perf_counter() - started) * 1000),
                    metadata={"message_count": result.get("message_count"), "last_message_id": result.get("last_message_id")},
                )
            except Exception:
                logger.exception("conversation_summary_refresh_failed user_id=%s", user_id)
            finally:
                self._summary_jobs.discard(user_id)

        try:
            asyncio.create_task(runner(), name=f"conversation-summary-refresh-{user_id}")
        except RuntimeError:
            self._summary_jobs.discard(user_id)

    def _normalize_usage(self, usage: dict[str, int | None], input_tokens: int, output_text: str) -> dict[str, int | None]:
        result = dict(usage)
        prompt = result.get("prompt_tokens")
        completion = result.get("completion_tokens")
        total = result.get("total_tokens")
        if prompt is None:
            prompt = input_tokens
        if completion is None:
            completion = estimate_tokens(output_text)
        if total is None:
            total = int(prompt) + int(completion)
        result["prompt_tokens"] = int(prompt)
        result["completion_tokens"] = int(completion)
        result["total_tokens"] = int(total)
        return result

    def _trace_step_ms(self, trace: RequestTrace, name: str) -> int | None:
        for step in reversed(trace.steps):
            if step.get("name") == name:
                return int(step.get("elapsed_ms") or 0)
        return None

    def _compact_user_profile(self, profile) -> dict[str, str]:
        if profile is None:
            return {}
        result = {
            "display_name": self._compact(str(getattr(profile, "display_name", "") or ""), 60),
            "language_code": self._compact(str(getattr(profile, "language_code", "") or ""), 12),
        }
        return {key: value for key, value in result.items() if value}

    def _estimate_message_tokens(self, messages: list[dict[str, str]]) -> int:
        return sum(estimate_tokens(message.get("content", "")) for message in messages)

    def _reply_text(self, reply: NargesReply) -> str:
        return "\n".join(message.text for message in reply.messages if message.text)

    def _keywords(self, text: str) -> set[str]:
        normalized = (text or "").lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
        return set(re.findall(r"[\w\u0600-\u06FF]{3,}", normalized))

    def _format_debug_blocks(
        self,
        *,
        result: ProviderResult,
        input_token_estimate: int,
        compiled_sections: tuple[str, ...],
        message_datetime: datetime,
        provider_failed: bool = False,
    ) -> str:
        payload = {
            "provider_failed": provider_failed,
            "sections": list(compiled_sections),
            "message_datetime": message_datetime.isoformat(),
            "provider": result.provider,
            "model": result.model,
            "estimated_input_tokens": input_token_estimate,
            "usage": result.usage,
        }
        return "\n\n```narges-debug\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str)[:1800] + "\n```"

    def _fallback_reply(self, long_user_message: bool = False) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SHORT,
            messages=[
                TelegramOutboundMessage(
                    text="پیامت خیلی بلند بود؛ بخش اصلیش رو کوتاه‌تر بفرست." if long_user_message else "الان سرویس جواب نداد؛ دوباره بفرست.",
                    delay_seconds=0.1,
                )
            ],
        )

    def _quota_fallback_reply(self) -> NargesReply:
        return NargesReply(
            mode=ResponseMode.SHORT,
            messages=[
                TelegramOutboundMessage(
                    text="باقی‌ماندهٔ امروزت برای این جواب کافی نیست. پیام کوتاه‌تری بفرست یا بعداً دوباره امتحان کن.",
                    delay_seconds=0.1,
                )
            ],
        )

    def _compact(self, value: str, limit: int) -> str:
        compact = " ".join((value or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"

    def _compact_multiline(self, value: str, char_limit: int, line_limit: int) -> str:
        lines = [" ".join(line.split()) for line in (value or "").splitlines() if line.strip()]
        compact = "\n".join(lines[:line_limit])
        if len(compact) <= char_limit:
            return compact
        return compact[: char_limit - 1].rstrip() + "…"

    def _utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
