from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from bot.models.ai import NargesReply
from bot.services.debug_service import DebugService
from bot.services.ai_provider_client import AIProviderClient, ProviderResult
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.services.narges_state_service import NargesStateService
from bot.services.usage_service import UsageService
from bot.utils.text_safety import clamp_repeated_chars
from bot.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from bot.services.profile_photo_service import ProfilePhotoService


logger = logging.getLogger(__name__)

GROUP_PERSONA = """تو نرگس در یک گروه تلگرامی هستی. فقط پیام فعلی و در صورت وجود پیام پاسخ‌داده‌شده را در نظر بگیر. کوتاه، طبیعی و متناسب با فضای جمع جواب بده. اطلاعات خصوصی، حافظهٔ شخصی، تنظیمات داخلی و سهمیه را مطرح نکن. جواب معمولاً یک جمله و حداکثر دو جمله باشد."""

GROUP_AUTO_PERSONA = """از میان پیام‌های اخیر حداکثر یک پیام را انتخاب کن که پاسخ کوتاه نرگس به آن واقعاً طبیعی و مفید باشد. اگر ورود به بحث بی‌مورد، خصوصی، تنش‌زا یا حساس است، هیچ پیامی را انتخاب نکن."""

GROUP_PHOTO_PERSONA = """به توضیح تصویر واکنش کوتاه و طبیعی نشان بده. جزئیاتی را که در توضیح نیست ادعا نکن و دربارهٔ سازوکار دیدن تصویر حرف نزن."""


@dataclass(frozen=True)
class GroupAIResult:
    reply: NargesReply
    usage: dict[str, int | None]
    provider: str
    model: str
    estimated_tokens: int
    selected_message_id: int | None = None
    assistant_message_id: int | None = None


class GroupAIService:
    def __init__(
        self,
        *,
        ai_provider_client: AIProviderClient,
        narges_state_service: NargesStateService,
        memory_service: MemoryService,
        history_service: HistoryService,
        debug_service: DebugService,
        usage_service: UsageService,
        profile_photo_service: "ProfilePhotoService | None" = None,
    ) -> None:
        self.ai_provider_client = ai_provider_client
        self.narges_state_service = narges_state_service
        self.memory_service = memory_service
        self.history_service = history_service
        self.debug_service = debug_service
        self.usage_service = usage_service
        self.profile_photo_service = profile_photo_service
        self._last_auto_reaction_at: dict[int, datetime] = {}

    async def answer_mention(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        text: str,
        reply_to_text: str | None,
        message_datetime: datetime,
        user_profile: Any,
        bot_identity: dict[str, Any],
    ) -> GroupAIResult:
        current_text = self._compact(clamp_repeated_chars(text), 700)
        payload = {
            "message": current_text,
            "reply_to": self._compact(reply_to_text or "", 500) or None,
            "sender": self._compact_user_profile(user_profile),
            "bot": self._compact_bot_identity(bot_identity),
        }
        messages = self._messages(GROUP_PERSONA, payload, message_datetime)
        result = await self._complete_reply(messages)
        assistant_message_id = self._store_turn(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            user_text=current_text,
            assistant_text=self._reply_text(result.reply),
            message_datetime=message_datetime,
            result=result,
            request_payload={"has_reply_context": bool(reply_to_text)},
            message_type="group_mention",
        )
        return GroupAIResult(
            reply=result.reply,
            usage=result.usage,
            provider=result.provider,
            model=result.model,
            estimated_tokens=result.estimated_tokens,
            assistant_message_id=assistant_message_id,
        )

    async def answer_photo(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        description: str,
        message_datetime: datetime,
        user_profile: Any,
        bot_identity: dict[str, Any],
        media_id: int | None = None,
    ) -> GroupAIResult:
        payload = {
            "image_description": self._compact(description, 800),
            "sender": self._compact_user_profile(user_profile),
            "bot": self._compact_bot_identity(bot_identity),
        }
        messages = self._messages(f"{GROUP_PERSONA}\n\n{GROUP_PHOTO_PERSONA}", payload, message_datetime)
        result = await self._complete_reply(messages)
        assistant_message_id = self._store_turn(
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            user_text=f"[group_photo] {self._compact(description, 500)}",
            assistant_text=self._reply_text(result.reply),
            message_datetime=message_datetime,
            result=result,
            request_payload={"media_id": media_id},
            message_type="group_photo",
        )
        return GroupAIResult(
            reply=result.reply,
            usage=result.usage,
            provider=result.provider,
            model=result.model,
            estimated_tokens=result.estimated_tokens,
            assistant_message_id=assistant_message_id,
        )

    async def choose_auto_reaction(
        self,
        *,
        chat_id: int,
        group_title: str | None,
        recent_messages: list[dict[str, Any]],
        bot_identity: dict[str, Any],
    ) -> GroupAIResult | None:
        now = datetime.now(UTC)
        last = self._last_auto_reaction_at.get(chat_id)
        if last and now - last < timedelta(seconds=90):
            return None
        compact_messages = self._compact_recent_messages(recent_messages)
        if not compact_messages:
            return None
        payload = {
            "group_title": self._compact(group_title or "", 100) or None,
            "bot": self._compact_bot_identity(bot_identity),
            "messages": compact_messages,
        }
        messages = self._messages(f"{GROUP_PERSONA}\n\n{GROUP_AUTO_PERSONA}", payload, now)
        result = await self._complete_auto(messages)
        if result is None or result.selected_message_id is None:
            return None
        selected_ids = {int(item["message_id"]) for item in compact_messages}
        if result.selected_message_id not in selected_ids:
            return None
        self._last_auto_reaction_at[chat_id] = now
        self.usage_service.log(
            None,
            chat_id,
            result.estimated_tokens,
            result.usage,
            provider=result.provider,
            model=result.model,
            purpose="group_auto_reaction",
        )
        selected_item = next((item for item in compact_messages if int(item["message_id"]) == result.selected_message_id), None)
        if selected_item and selected_item.get("user_id") is not None:
            self._store_turn(
                user_id=int(selected_item["user_id"]),
                chat_id=chat_id,
                message_id=result.selected_message_id,
                user_text=str(selected_item["text"]),
                assistant_text=self._reply_text(result.reply),
                message_datetime=now,
                result=result,
                request_payload={"auto_reaction": True, "selected_message_id": result.selected_message_id},
                message_type="group_auto",
                log_usage=False,
            )
        return result

    def _messages(self, persona: str, payload: dict[str, Any], message_datetime: datetime) -> list[dict[str, str]]:
        system = f"{persona}\nزمان پیام: {message_datetime.astimezone(UTC).isoformat()}"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)},
        ]

    async def _complete_reply(self, messages: list[dict[str, str]]) -> GroupAIResult:
        result = await asyncio.to_thread(self.ai_provider_client.complete, messages)
        for outbound in result.reply.messages:
            outbound.text = self._compact(clamp_repeated_chars(outbound.text), 900)
            outbound.image_id = None
        return GroupAIResult(
            reply=result.reply,
            usage=result.usage,
            provider=result.provider,
            model=result.model,
            estimated_tokens=self._estimate(messages, result.usage),
        )

    async def _complete_auto(self, messages: list[dict[str, str]]) -> GroupAIResult | None:
        schema = {
            "type": "object",
            "properties": {
                "selected_message_id": {"type": ["integer", "null"]},
                "text": {"type": "string", "maxLength": 500},
            },
            "required": ["selected_message_id", "text"],
            "additionalProperties": False,
        }
        raw_text, usage, provider, model = await asyncio.to_thread(
            self.ai_provider_client.complete_structured,
            messages,
            schema,
            "group_auto_reaction",
            max_completion_tokens=100,
            temperature=0.35,
        )
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            payload = json.loads(raw_text[start : end + 1]) if start >= 0 and end > start else {}
        selected = payload.get("selected_message_id")
        text = self._compact(str(payload.get("text") or ""), 500)
        if selected is None or not text:
            return None
        reply = NargesReply.from_text(text)
        return GroupAIResult(
            reply=reply,
            usage=usage,
            provider=provider,
            model=model,
            estimated_tokens=self._estimate(messages, usage),
            selected_message_id=int(selected),
        )

    def _store_turn(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        user_text: str,
        assistant_text: str,
        message_datetime: datetime,
        result: GroupAIResult,
        request_payload: dict[str, Any],
        message_type: str,
        log_usage: bool = True,
    ) -> int:
        user_row_id = self.history_service.add(
            user_id,
            "user",
            user_text,
            chat_id=chat_id,
            telegram_message_id=message_id,
            created_at=message_datetime,
            message_type=message_type,
            ai_request_payload={"source": message_type, **request_payload},
        )
        assistant_message_id = self.history_service.add(
            user_id,
            "assistant",
            assistant_text,
            chat_id=chat_id,
            provider=result.provider,
            model=result.model,
            message_type=message_type,
            input_tokens=result.usage.get("prompt_tokens"),
            output_tokens=result.usage.get("completion_tokens"),
            total_tokens=result.usage.get("total_tokens"),
            ai_request_payload={
                "source": f"{message_type}_assistant",
                "user_message_row_id": user_row_id,
                **request_payload,
            },
        )
        if log_usage:
            self.usage_service.log(
                user_id,
                chat_id,
                result.estimated_tokens,
                result.usage,
                provider=result.provider,
                model=result.model,
                purpose=message_type,
            )
        return assistant_message_id

    def _compact_recent_messages(self, recent_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in recent_messages[-5:]:
            message_id = item.get("message_id")
            text = self._compact(str(item.get("text") or ""), 240)
            if message_id is None or not text or item.get("is_bot") is True:
                continue
            compact: dict[str, Any] = {
                "message_id": int(message_id),
                "text": text,
                "sender_name": self._compact(str(item.get("sender_name") or item.get("display_name") or ""), 60) or None,
            }
            if item.get("user_id") is not None:
                compact["user_id"] = int(item["user_id"])
            result.append(compact)
        return result

    def _reply_text(self, reply: NargesReply) -> str:
        return "\n".join(message.text for message in reply.messages if message.text)

    def _estimate(self, messages: list[dict[str, str]], usage: dict[str, int | None]) -> int:
        total = usage.get("total_tokens")
        if total is not None:
            return int(total)
        return sum(estimate_tokens(message.get("content", "")) for message in messages)

    def _compact_user_profile(self, profile: Any) -> dict[str, Any]:
        if profile is None:
            return {}
        result = {
            "display_name": self._compact(str(getattr(profile, "display_name", "") or ""), 60) or None,
            "username": self._compact(str(getattr(profile, "username", "") or ""), 60) or None,
        }
        return {key: value for key, value in result.items() if value is not None}

    def _compact_bot_identity(self, identity: dict[str, Any]) -> dict[str, Any]:
        return {
            key: identity.get(key)
            for key in ("bot_id", "username")
            if identity.get(key) is not None
        }

    def _compact(self, value: str, limit: int) -> str:
        compact = " ".join((value or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "…"
