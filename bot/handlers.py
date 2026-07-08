import asyncio
import json
import logging
import mimetypes
import re
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardRemove,
    Update,
    User,
)
try:
    from aiogram.types import ReplyParameters
except Exception:  # pragma: no cover - depends on aiogram minor version
    ReplyParameters = None  # type: ignore[assignment]
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from bot.config import Settings
from bot.models.channel import MembershipCheck
from bot.models.user import OnboardingState, TelegramUserProfile
from bot.services.billing_service import BillingService
from bot.services.chat_service import ChatService, UserFacingError
from bot.services.debug_service import DebugService
from bot.services.group_ai_service import GroupAIService
from bot.services.group_service import GroupInviteRewardService, GroupService
from bot.services.history_service import HistoryService
from bot.services.media_service import (
    UNSUPPORTED_AUDIO_MESSAGE,
    UNSUPPORTED_MEDIA_MESSAGE,
    VISION_ERROR_MESSAGE,
    MediaStorageError,
    MediaStorageService,
    StoredMedia,
    BotImageCatalog,
    VisionClient,
)
from bot.services.memory_service import MemoryService
from bot.services.menu_service import MenuService
from bot.services.moderation_service import ModerationService
from bot.services.narges_state_service import NargesStateService
from bot.services.name_service import NameService
from bot.services.profile_photo_service import ProfilePhotoService
from bot.services.quota_service import QuotaService
from bot.services.required_channel_service import RequiredChannelService
from bot.storage.orm import ConversationMessageORM, UserORM
from bot.services.request_trace import RequestTrace
from bot.services.user_service import UserService
from bot.utils.text_safety import clamp_repeated_chars, meaningful_length


logger = logging.getLogger(__name__)


def register_handlers(
    dispatcher: Dispatcher,
    chat_service: ChatService,
    memory_service: MemoryService,
    user_service: UserService,
    channel_service: RequiredChannelService,
    name_service: NameService,
    menu_service: MenuService,
    quota_service: QuotaService,
    billing_service: BillingService,
    moderation_service: ModerationService,
    history_service: HistoryService,
    narges_state_service: NargesStateService,
    debug_service: DebugService,
    group_service: GroupService | None,
    group_invite_reward_service: GroupInviteRewardService | None,
    group_ai_service: GroupAIService | None,
    media_storage_service: MediaStorageService,
    bot_image_catalog: BotImageCatalog,
    vision_client: VisionClient,
    profile_photo_service: ProfilePhotoService,
    settings: Settings,
) -> None:
    admin_ids = set(settings.admin_ids)

    def profile_from_user(user: User) -> TelegramUserProfile:
        return TelegramUserProfile(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
        )

    def is_admin(user_id: int) -> bool:
        return user_id in admin_ids

    def can_debug(user_id: int) -> bool:
        return debug_service.can_debug(user_id)

    async def keep_typing(bot: Bot, chat_id: int, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4)
            except asyncio.TimeoutError:
                continue

    def is_same_name_request(text: str) -> bool:
        value = (text or "").strip().lower()
        return value in {"همین", "همین خوبه", "همین صدام کن", "همینو صدام کن", "باشه", "اوکی", "yes", "ok"}

    def gender_label(gender: str | None) -> str:
        return {"female": "دختر", "male": "پسر", "unspecified": "ثبت نشده"}.get(gender or "", "ثبت نشده")

    def format_quota_units(units: int) -> str:
        if units % 5 == 0:
            return str(units // 5)
        return f"{units / 5:.1f}"

    def phone_bonus_available(profile) -> bool:
        return not bool(profile and (profile.phone_number or profile.phone_bonus_claimed))

    def capacity_text(profile=None) -> str:
        lines = [
            "✨ افزایش ظرفیت",
            "",
            "یکی را انتخاب کن:",
            "🎁 دعوت دوستان (پیام رایگان)",
            "⭐ افزایش با Stars",
            "💳 خرید پیام",
        ]
        if phone_bonus_available(profile):
            lines.append("📱 افزایش با شماره موبایل")
        return "\n".join(lines)

    def schedule_profile_photo_sync(bot: Bot, user: User | None) -> None:
        if user and not user.is_bot:
            profile_photo_service.schedule_sync(bot, user.id)

    def format_toman(value: int) -> str:
        return f"{value:,}".replace(",", "٬")

    def card_payment_text(invoice) -> str:
        plan = billing_service.get_plan(invoice.plan_id)
        card = settings.billing_card_number or "شماره کارت در .env تنظیم نشده"
        amount = plan.toman_cost if plan else invoice.toman_cost
        return (
            "💳 فاکتور خرید پیام\n\n"
            f"📦 بسته: {invoice.message_quota} پیام\n"
            f"💰 مبلغ: {format_toman(amount)} تومان\n"
            f"🧾 شماره فاکتور: `{invoice.invoice_id}`\n\n"
            "مبلغ را به کارت زیر واریز کن:\n"
            f"`{card}`\n\n"
            "بعد از پرداخت، عکس رسید یا شماره پیگیری را همینجا بفرست.\n"
            "بعد از بررسی ادمین، نتیجه را همینجا می‌فرستم."
        )

    def looks_like_receipt_message(text: str) -> bool:
        lowered = (text or "").strip().lower()
        if any(word in lowered for word in ("رسید", "پیگیری", "واریز", "کارت به کارت", "پرداخت", "receipt", "paid")):
            return True
        digits = "".join(char for char in lowered if char.isdigit())
        return len(digits) >= 6

    def jsonable(value):
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if is_dataclass(value) and not isinstance(value, type):
            return asdict(value)
        if isinstance(value, list):
            return [jsonable(item) for item in value]
        if isinstance(value, tuple):
            return [jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        return value

    async def send_json(message: Message, payload: dict) -> None:
        text = json.dumps(jsonable(payload), ensure_ascii=False, indent=2, default=str)
        await message.answer(f"```json\n{text[:3800]}\n```")

    async def send_chunks(message: Message, text: str, limit: int = 3600) -> None:
        text = text.strip()
        if not text:
            return
        while text:
            chunk = text[:limit]
            if len(text) > limit and "\n" in chunk:
                chunk = chunk[: chunk.rfind("\n")]
            await message.answer(chunk)
            text = text[len(chunk):].lstrip()

    def command_args(message: Message) -> str:
        return (message.text or "").split(maxsplit=1)[1].strip() if len((message.text or "").split(maxsplit=1)) == 2 else ""

    async def require_admin_command(message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        if is_admin(user_id):
            return True
        await message.answer("این دستور فقط برای ادمین است.")
        return False

    def is_group_chat_type(chat_type: str | None) -> bool:
        return chat_type in {"group", "supergroup"}

    def group_text(message: Message) -> str:
        return clamp_repeated_chars((message.text or message.caption or "").strip())

    def compact_reply_context(message: Message) -> str | None:
        replied = message.reply_to_message
        if not replied:
            return None
        author = getattr(replied.from_user, "username", None) or getattr(replied.from_user, "first_name", None) or "unknown"
        text = clamp_repeated_chars((replied.text or replied.caption or "").strip())
        if not text:
            text = f"[{replied.content_type}]"
        return f"پیام ریپلای‌شده که کاربر نرگس را روی آن صدا کرده است: {author}: {text[:700]}"

    def is_narges_mentioned(message: Message, bot_username: str = "narges_aibot", bot_id: int | None = None) -> bool:
        text = group_text(message)
        lowered = text.lower()
        username = (bot_username or "narges_aibot").strip().lower().lstrip("@")
        normalized = re.sub(r"[\u200c\s_]+", "", lowered)
        if "نرگس" in normalized or f"@{username}" in lowered or "@narges_aibot" in lowered:
            return True
        for entity in (message.entities or message.caption_entities or []):
            entity_type = str(getattr(entity, "type", None) or "")
            if entity_type == "text_mention" and bot_id is not None:
                mentioned_user = getattr(entity, "user", None)
                if mentioned_user is not None and getattr(mentioned_user, "id", None) == bot_id:
                    return True
            if entity_type == "mention":
                offset = int(getattr(entity, "offset", 0) or 0)
                length = int(getattr(entity, "length", 0) or 0)
                mention = text[offset : offset + length].lower().lstrip("@")
                if mention in {username, "narges_aibot"}:
                    return True
        return False

    def is_reply_to_bot(message: Message, bot_id: int | None) -> bool:
        replied = message.reply_to_message
        return bool(replied and replied.from_user and bot_id is not None and replied.from_user.id == bot_id)

    def is_reasonable_group_mention(message: Message) -> bool:
        text = group_text(message)
        return bool(text and len(text) <= 900 and len(text.split()) <= 160)

    def is_standalone_group_photo(message: Message) -> bool:
        return bool(
            message.photo
            and not message.caption
            and not message.reply_to_message
        )

    async def bot_identity(bot: Bot) -> dict:
        try:
            me = await bot.get_me()
            return {
                "bot_id": me.id,
                "username": f"@{me.username}" if me.username else "@narges_aibot",
                "expected_username": "@narges_aibot",
            }
        except Exception:
            return {"bot_id": None, "username": "@narges_aibot", "expected_username": "@narges_aibot"}

    async def group_member_count(bot: Bot, chat_id: int) -> int | None:
        for method_name in ("get_chat_member_count", "get_chat_members_count"):
            method = getattr(bot, method_name, None)
            if method is None:
                continue
            try:
                return int(await method(chat_id))
            except Exception:
                logger.debug("group_member_count_unavailable chat_id=%s method=%s", chat_id, method_name, exc_info=True)
        return None

    async def track_group_message(message: Message, bot: Bot) -> None:
        if group_service is None or not is_group_chat_type(getattr(message.chat, "type", None)):
            return
        group_service.upsert_group(
            chat_id=message.chat.id,
            title=getattr(message.chat, "title", None),
            username=getattr(message.chat, "username", None),
            chat_type=str(message.chat.type),
            bot_status="member",
            member_count=await group_member_count(bot, message.chat.id),
            active=True,
        )

    async def delete_membership_gate(bot: Bot, user_id: int) -> None:
        profile = user_service.get(user_id)
        if not profile or not profile.last_membership_gate_chat_id or not profile.last_membership_gate_message_id:
            return
        with suppress(Exception):
            await bot.delete_message(profile.last_membership_gate_chat_id, profile.last_membership_gate_message_id)
        user_service.clear_membership_gate_message(user_id)

    async def delete_prompt_message(bot: Bot, user_id: int) -> None:
        profile = user_service.get(user_id)
        if not profile or not profile.last_prompt_chat_id or not profile.last_prompt_message_id:
            return
        with suppress(Exception):
            await bot.delete_message(profile.last_prompt_chat_id, profile.last_prompt_message_id)
        user_service.clear_prompt_message(user_id)

    async def show_membership_gate(
        message: Message,
        bot: Bot,
        check: MembershipCheck,
        verification_failed: bool = False,
        user_id: int | None = None,
    ) -> None:
        user_id = user_id or (message.from_user.id if message.from_user else 0)
        await delete_membership_gate(bot, user_id)
        note = (
            "\n\n⚠️ اگر عضو هستی ولی تأیید نمی‌شود، چند ثانیه بعد دوباره دکمه را بزن."
            if verification_failed
            else ""
        )
        text = (
            "📣 عضویت لازم است\n\n"
            "برای استفاده از نرگس، اول عضو کانال‌های زیر شو.\n"
            "بعد از عضویت، دکمه «✅ عضو شدم، بررسی کن» را بزن."
            f"{note}"
        )
        sent = await message.answer(text, reply_markup=menu_service.membership_keyboard(check))
        if user_id:
            user_service.set_membership_gate_message(user_id, sent.chat.id, sent.message_id)

    async def ensure_membership(message: Message, bot: Bot, use_cache: bool = True) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        check = await channel_service.check_user(bot, user_id, use_cache=use_cache)
        if check.ok:
            await delete_membership_gate(bot, user_id)
            user_service.mark_membership_ok(user_id)
            user_service.recover_registration_state(user_id)
            return True
        user_service.mark_membership_required(user_id)
        await show_membership_gate(message, bot, check)
        return False

    async def ensure_not_blocked_for_model(message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        if is_admin(user_id):
            return True
        status = moderation_service.get_block_status(user_id)
        if not status.blocked:
            return True
        history_service.add(
            user_id,
            "user",
            blocked_message_text(message),
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            created_at=message.date,
            message_type="blocked",
            ai_request_payload={
                "source": "blocked_gate",
                "sent_to_model": False,
                "content_type": str(message.content_type),
                "blocked_until": status.blocked_until.isoformat() if status.blocked_until else None,
            },
        )
        await message.answer(moderation_service.block_message(status))
        return False

    def blocked_message_text(message: Message) -> str:
        text = clamp_repeated_chars((message.text or message.caption or "").strip())
        if text:
            return text
        return f"[blocked_{message.content_type}]"

    async def ask_for_name(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        profile = user_service.get(user_id)
        suggestion = name_service.suggest_from_telegram(
            profile.first_name if profile else None,
            profile.username if profile else None,
        )
        user_service.set_suggested_name(user_id, suggestion)
        if suggestion:
            user_service.set_state(user_id, OnboardingState.ASK_NAME_CONFIRM)
            await message.answer(f"✨ دوست داری «{suggestion}» صدات کنم؟", reply_markup=menu_service.name_confirm())
            return
        user_service.set_state(user_id, OnboardingState.ASK_NAME_INPUT)
        await message.answer("🌸 چی صدات کنم؟\nفقط اسمت رو کوتاه و ساده بنویس.")

    async def ask_for_gender(message: Message, user_id: int, name: str | None = None) -> None:
        user_service.set_state(user_id, OnboardingState.ASK_GENDER)
        if name:
            await message.answer(
                f"✅ ثبت شد. از این به بعد «{name}» صدات می‌کنم.\n\nحالا برای اینکه لحنم دقیق‌تر باشه، جنسیتت رو انتخاب کن:",
                reply_markup=menu_service.gender_keyboard(),
            )
            return
        await message.answer("⚧️ جنسیتت رو انتخاب کن تا نرگس دقیق‌تر باهات حرف بزنه:", reply_markup=menu_service.gender_keyboard())

    async def finish_name_update(message: Message, user_id: int, name: str, bot: Bot | None = None) -> None:
        if bot:
            await delete_prompt_message(bot, user_id)
        profile = user_service.get(user_id)
        if profile and profile.registration_state == OnboardingState.READY:
            user_service.save_display_name_keep_state(user_id, name)
            user_service.set_onboarding_state(user_id, OnboardingState.READY)
            memory_service.upsert_identity_name(user_id, name, message.message_id)
            await message.answer(f"✅ از این به بعد «{name}» صدات می‌کنم.", reply_markup=menu_service.reply_menu(can_debug(user_id)))
            await show_account(message)
            return
        user_service.save_display_name(user_id, name)
        memory_service.upsert_identity_name(user_id, name, message.message_id)
        await ask_for_gender(message, user_id, name)

    async def finish_gender_update(message: Message, user_id: int, gender: str | None, bot: Bot | None = None) -> None:
        user_service.save_gender(user_id, gender)
        inviter_id = user_service.claim_referral_bonus_if_ready(user_id)
        if inviter_id:
            quota_service.add_extra_credit(inviter_id, 10, reason=f"referral:{user_id}")
            if bot:
                await notify_referral_reward(bot, inviter_id, user_id)
        await message.answer(
            "تمومه ✨\nاز الان هرچی بفرستی مستقیم می‌رسه به نرگس.",
            reply_markup=menu_service.reply_menu(can_debug(user_id)),
        )

    async def handle_name_text(message: Message, bot: Bot) -> bool:
        if not message.from_user:
            return False
        profile = user_service.get(message.from_user.id)
        if profile is None or profile.onboarding_state not in {
            OnboardingState.ASK_NAME_INPUT,
            OnboardingState.NAME_AMBIGUOUS_CONFIRM,
        }:
            return False

        if is_same_name_request(message.text or "") and (profile.pending_name or profile.suggested_name):
            await finish_name_update(message, message.from_user.id, profile.pending_name or profile.suggested_name, bot)  # type: ignore[arg-type]
            return True

        raw_name = (message.text or "").strip()
        result = name_service.validate(raw_name, allow_ambiguous=True)
        if not result.ok or not result.normalized:
            await message.answer(
                f"اسم ذخیره نشد 🌙\n{result.reason or ''}\n\nیه اسم فارسی کوتاه بنویس؛ مثلا: نرگس",
                reply_markup=menu_service.name_retry_keyboard(can_cancel=profile.registration_state == OnboardingState.READY),
            )
            return True
        if profile.registration_state != OnboardingState.READY and profile.onboarding_state == OnboardingState.ASK_NAME_INPUT and not profile.name_confirm_attempted:
            user_service.set_pending_name(message.from_user.id, raw_name, attempted=True)
            user_service.set_state(message.from_user.id, OnboardingState.NAME_AMBIGUOUS_CONFIRM)
            await message.answer(
                f"دوست داری «{raw_name}» صدات کنم؟",
                reply_markup=menu_service.ambiguous_name_confirm(),
            )
            return True
        if profile.registration_state != OnboardingState.READY and result.ambiguous and not profile.name_confirm_attempted:
            user_service.set_pending_name(message.from_user.id, raw_name, attempted=True)
            user_service.set_state(message.from_user.id, OnboardingState.NAME_AMBIGUOUS_CONFIRM)
            await message.answer(
                f"دوست داری «{raw_name}» صدات کنم؟",
                reply_markup=menu_service.ambiguous_name_confirm(),
            )
            return True
        await finish_name_update(message, message.from_user.id, result.normalized, bot)
        return True

    async def show_memories(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not can_debug(user_id):
            await message.answer("🧠 بخش حافظه فقط در حالت debug فعاله.")
            return
        memories = memory_service.list_active(user_id, limit=30)
        if not memories:
            await message.answer("🧠 حافظه‌ها\n\n✨ هنوز چیزی برایت ذخیره نکرده‌ام.")
            return
        lines = ["🧠 حافظه‌های فعال", ""]
        lines.extend(f"🔹 #{item.id} | {item.kind.value}\n{item.summary}" for item in memories)
        await message.answer("\n".join(lines))

    async def show_account(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        profile = user_service.get(user_id)
        quota = quota_service.account_quota(user_id)
        block = moderation_service.get_block_status(user_id)
        if profile is None:
            await message.answer("👤 پروفایل\n\n⚠️ اول /start را بزن تا حساب کاربری‌ات ساخته شود.")
            return
        block_line = "فعال نیست"
        if block.blocked and block.blocked_until:
            block_line = f"مسدود تا {block.blocked_until.strftime('%Y-%m-%d %H:%M UTC')}"
        await message.answer(
            "👤 پروفایل کاربر\n\n"
            f"🏷️ نام: {profile.display_name or 'ثبت نشده'}\n"
            f"⚧️ جنسیت: {gender_label(profile.gender)}\n"
            f"📨 مصرف کل: {format_quota_units(quota.total_sent)} پیام\n"
            f"☀️ باقی‌مانده روزانه: {format_quota_units(quota.daily_remaining)} از {quota.daily_limit}\n"
            f"🗓️ باقی‌مانده ماهانه: {format_quota_units(quota.monthly_remaining)} از {quota.monthly_limit}\n"
            f"⚡ ظرفیت اضافه: {format_quota_units(quota.extra_remaining)} پیام\n"
            f"📱 شماره موبایل: {'✅ ثبت شده' if profile.phone_number else '❌ ثبت نشده'}\n"
            f"🛡️ وضعیت مسدودی: {block_line}",
            reply_markup=menu_service.account_keyboard(can_debug(user_id)),
        )

    async def show_referral(message: Message, bot: Bot) -> None:
        user_id = message.from_user.id if message.from_user else 0
        stats = user_service.referral_stats(user_id)
        bot_username = (await bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={stats['code']}" if bot_username else stats["code"]
        await message.answer(
            "🎁 دعوت دوستان\n\n"
            "لینک اختصاصی‌ات:\n"
            f"`{link}`\n\n"
            "هر دوستی که با لینک تو بیاد، پروفایلش رو کامل کنه و حداقل یک سوال بپرسه، "
            "۱۰ پیام اضافه  به ظرفیتت اضافه می‌شه.\n\n"
            f"👥 دعوت‌شده‌ها: {stats['total']}\n"
            f"✅ کامل‌شده: {stats['qualified']}\n"
            f"⭐ پاداش‌گرفته: {stats['rewarded']}",
        )

    async def notify_referral_reward(bot: Bot, inviter_id: int, invited_user_id: int) -> None:
        with suppress(Exception):
            await bot.send_message(
                inviter_id,
                "🎁 دعوتت کامل شد!\n\n"
                "یکی از دوستات پروفایلش رو کامل کرد و اولین سوالش رو پرسید.\n"
                "✅ ۱۰ پیام رایگان به ظرفیتت اضافه شد.\n\n"
                f"👤 کاربر دعوت‌شده: `{invited_user_id}`",
            )

    def image_model_text(description: str, caption: str | None, duplicate_context: dict | None = None) -> str:
        caption_text = clamp_repeated_chars((caption or "").strip())
        duplicate_note = ""
        if duplicate_context:
            duplicate_note = (
                "\nنکته حافظه تصویری: همین فایل/عکس با hash یکسان قبلا هم به ربات داده شده بود. "
                "در جواب طبیعی و کوتاه می‌توانی اشاره کنی که این عکس را قبلا هم فرستاده بود."
            )
        if caption_text:
            return (
                "کاربر یک عکس ارسال کرد.\n"
                f"محتوای عکس: {description.strip()}\n"
                f"متن همراه کاربر: {caption_text}"
                f"{duplicate_note}"
            )
        return (
            "کاربر یک عکس ارسال کرد.\n"
            f"محتوای عکس: {description.strip()}"
            f"{duplicate_note}"
        )

    def media_storage_error_message(exc: Exception) -> str:
        if "too large" in str(exc).lower():
            return "این فایل زیادی بزرگه و امن نیست که ذخیره‌اش کنم."
        return UNSUPPORTED_MEDIA_MESSAGE

    async def retry_failed_chat_turn_later(
        *,
        bot: Bot,
        user_id: int,
        chat_id: int,
        message_id: int,
        text_value: str,
        profile,
    ) -> None:
        await asyncio.sleep(10 * 60)
        try:
            result = await chat_service.answer(
                user_id,
                chat_id,
                message_id,
                text_value,
                datetime.now(UTC),
                user_profile=profile,
            )
            if getattr(result, "provider_failed", False):
                return
            for item in result.reply.messages:
                await bot.send_message(chat_id=chat_id, text=item.text)
                await asyncio.sleep(min(float(item.delay_seconds or 0), 0.35))
        except Exception:
            logger.exception("delayed_failed_turn_retry_failed user_id=%s chat_id=%s", user_id, chat_id)

    async def run_chat_turn(message: Message, bot: Bot, text_value: str, profile, reserved_quota_check=None, allow_error_retry: bool = True):
        trace = RequestTrace(
            "handler_chat_turn",
            {
                "user_id": message.from_user.id,
                "chat_id": message.chat.id,
                "telegram_message_id": message.message_id,
                "has_reserved_quota": reserved_quota_check is not None,
            },
        )
        stop_typing = asyncio.Event()
        with trace.step("start_typing"):
            typing_task = asyncio.create_task(keep_typing(bot, message.chat.id, stop_typing))
        try:
            with trace.step("chat_service_answer"):
                result = await chat_service.answer(
                    message.from_user.id,
                    message.chat.id,
                    message.message_id,
                    text_value,
                    message.date,
                    user_profile=profile,
                    reserved_quota_check=reserved_quota_check,
                    trace=trace,
                )
                if allow_error_retry and getattr(result, "provider_failed", False) and not is_group_chat_type(getattr(message.chat, "type", None)):
                    asyncio.create_task(
                        retry_failed_chat_turn_later(
                            bot=bot,
                            user_id=message.from_user.id,
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                            text_value=text_value,
                            profile=profile,
                        )
                    )
                return result
        except UserFacingError as exc:
            text = str(exc)
            stop_typing.set()
            with suppress(asyncio.CancelledError):
                await typing_task
            if any(word in text for word in ("سهمیه", "ظرفیت", "تند", "سقف", "limit")):
                current_profile = user_service.get(message.from_user.id)
                await message.answer(text, reply_markup=menu_service.capacity_keyboard(phone_bonus_available(current_profile)))
            else:
                await message.answer(text)
            return None
        finally:
            stop_typing.set()
            with suppress(asyncio.CancelledError):
                await typing_task
            debug_service.trace(
                "request_trace",
                trace.finish(phase="handler_chat_turn", content_chars=len(text_value)),
                user_id=message.from_user.id,
            )

    async def deliver_chat_result(message: Message, bot: Bot, result, profile) -> None:
        trace = RequestTrace(
            "telegram_delivery",
            {
                "user_id": message.from_user.id,
                "chat_id": message.chat.id,
                "telegram_message_id": message.message_id,
                "assistant_message_id": getattr(result, "assistant_message_id", None),
            },
        )
        logger.info(
            "answer_ready user_id=%s chat_id=%s estimated_tokens=%s total_tokens=%s",
            message.from_user.id,
            message.chat.id,
            result.estimated_tokens,
            result.usage.get("total_tokens"),
        )
        try:
            for index, item in enumerate(result.reply.messages):
                with trace.step("message_delay", index=index):
                    await asyncio.sleep(min(float(item.delay_seconds or 0), 0.35))
                if item.image_id:
                    with trace.step("send_photo", index=index, image_id=item.image_id):
                        sent_message_id = await send_photo_with_retries(message, bot, item.image_id, item.text, trace=trace)
                    history_service.set_telegram_message_id(getattr(result, "assistant_message_id", None), sent_message_id)
                    if sent_message_id:
                        bot_image_catalog.record_sent_image(
                            image_id=item.image_id,
                            user_id=message.from_user.id,
                            chat_id=message.chat.id,
                            telegram_message_id=sent_message_id,
                            caption=item.text,
                        )
                else:
                    with trace.step("send_text", index=index):
                        sent_message_id = await send_text_with_retries(message, item.text, bot, trace=trace)
                    history_service.set_telegram_message_id(getattr(result, "assistant_message_id", None), sent_message_id)
            if profile and not profile.gender:
                today = message.date.astimezone(UTC).date().isoformat()
                if user_service.should_send_gender_nudge(message.from_user.id, today):
                    with trace.step("send_gender_nudge"):
                        await message.answer(
                            "✨ یه پیشنهاد کوچولو\n\n"
                            "اگه جنسیتت رو تنظیم کنی، لحن جواب‌ها دقیق‌تر و طبیعی‌تر می‌شه.\n"
                            "هر وقت خواستی از همین دکمه عوضش کن 💫",
                            reply_markup=menu_service.gender_keyboard(),
                        )
            user_service.mark_first_question(message.from_user.id)
            inviter_id = user_service.claim_referral_bonus_if_ready(message.from_user.id)
            if inviter_id:
                quota_service.add_extra_credit(inviter_id, 10, reason=f"referral:{message.from_user.id}")
                with trace.step("notify_referral_reward"):
                    await notify_referral_reward(bot, inviter_id, message.from_user.id)
        finally:
            debug_service.trace(
                "request_trace",
                trace.finish(phase="telegram_delivery"),
                user_id=message.from_user.id,
            )

    async def send_text_with_retries(
        message: Message,
        text: str,
        bot: Bot | None = None,
        attempts: int = 5,
        trace: RequestTrace | None = None,
    ) -> int | None:
        last_error: Exception | None = None
        should_reply = is_group_chat_type(getattr(message.chat, "type", None))
        for attempt in range(1, attempts + 1):
            attempt_started = asyncio.get_running_loop().time()
            try:
                if should_reply and bot is not None:
                    if ReplyParameters is not None:
                        sent = await bot.send_message(
                            chat_id=message.chat.id,
                            text=text,
                            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
                        )
                    else:
                        sent = await bot.send_message(
                            chat_id=message.chat.id,
                            text=text,
                            reply_to_message_id=message.message_id,
                            allow_sending_without_reply=True,
                        )
                else:
                    sent = await message.answer(text)
                if trace:
                    trace.add(
                        "send_text_attempt",
                        int((asyncio.get_running_loop().time() - attempt_started) * 1000),
                        attempt=attempt,
                        status="sent",
                        telegram_message_id=sent.message_id,
                    )
                return int(sent.message_id)
            except Exception as exc:
                last_error = exc
                if trace:
                    trace.add(
                        "send_text_attempt",
                        int((asyncio.get_running_loop().time() - attempt_started) * 1000),
                        attempt=attempt,
                        status="failed",
                        error=f"{exc.__class__.__name__}: {exc}",
                    )
                logger.warning("answer_send_failed attempt=%s chat_id=%s error=%s", attempt, message.chat.id, exc)
                await asyncio.sleep(min(0.5 * attempt, 2))
        logger.error("answer_send_exhausted chat_id=%s error=%s", message.chat.id, last_error)
        return None

    async def send_photo_with_retries(
        message: Message,
        bot: Bot,
        image_id: str,
        caption: str,
        attempts: int = 5,
        trace: RequestTrace | None = None,
    ) -> int | None:
        payload = bot_image_catalog.payload(image_id)
        if payload is None:
            return await send_text_with_retries(message, caption, bot)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            attempt_started = asyncio.get_running_loop().time()
            try:
                file = BufferedInputFile(payload.content, filename=payload.filename)
                if is_group_chat_type(getattr(message.chat, "type", None)):
                    if ReplyParameters is not None:
                        sent = await bot.send_photo(
                            chat_id=message.chat.id,
                            photo=file,
                            caption=caption[:1024] or payload.description[:1024],
                            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
                        )
                    else:
                        sent = await bot.send_photo(
                            chat_id=message.chat.id,
                            photo=file,
                            caption=caption[:1024] or payload.description[:1024],
                            reply_to_message_id=message.message_id,
                            allow_sending_without_reply=True,
                        )
                else:
                    sent = await bot.send_photo(
                        chat_id=message.chat.id,
                        photo=file,
                        caption=caption[:1024] or payload.description[:1024],
                    )
                if trace:
                    trace.add(
                        "send_photo_attempt",
                        int((asyncio.get_running_loop().time() - attempt_started) * 1000),
                        attempt=attempt,
                        status="sent",
                        telegram_message_id=sent.message_id,
                        image_id=image_id,
                    )
                return int(sent.message_id)
            except Exception as exc:
                last_error = exc
                if trace:
                    trace.add(
                        "send_photo_attempt",
                        int((asyncio.get_running_loop().time() - attempt_started) * 1000),
                        attempt=attempt,
                        status="failed",
                        image_id=image_id,
                        error=f"{exc.__class__.__name__}: {exc}",
                    )
                logger.warning("photo_send_failed image_id=%s attempt=%s chat_id=%s error=%s", image_id, attempt, message.chat.id, exc)
                await asyncio.sleep(min(0.5 * attempt, 2))
        logger.error("photo_send_exhausted image_id=%s chat_id=%s error=%s", image_id, message.chat.id, last_error)
        return await send_text_with_retries(message, caption, bot, attempts=attempts, trace=trace)

    async def handle_ready_image(message: Message, bot: Bot, stored_media: StoredMedia, profile) -> None:
        matched_bot_image = bot_image_catalog.matching_bot_image(stored_media)
        if not matched_bot_image and media_storage_service.image_count_today(message.from_user.id) > settings.image_daily_limit:
            record_media_message(message, stored_media, "image daily limit reached")
            await message.answer(VISION_ERROR_MESSAGE)
            return
        if not await ensure_not_blocked_for_model(message):
            return
        quota_check = await quota_service.begin_generation(message.from_user.id)
        if not quota_check.ok:
            await message.answer(quota_check.message)
            return
        try:
            with suppress(Exception):
                await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            duplicate_context = media_storage_service.duplicate_context(stored_media)
            try:
                if matched_bot_image:
                    description = "کاربر عکس نرگس را ارسال کرده است."
                    media_storage_service.set_vision_description(stored_media.id, description)
                    debug_service.trace(
                        "media_matches_bot_image",
                        {
                            "media_id": stored_media.id,
                            "catalog_id": matched_bot_image.get("catalog_id"),
                            "content_hash": stored_media.content_hash,
                            "telegram_message_id": stored_media.telegram_message_id,
                        },
                        user_id=message.from_user.id,
                    )
                elif cached_description := media_storage_service.cached_vision_description(stored_media):
                    description = cached_description
                    media_storage_service.set_vision_description(stored_media.id, description)
                    debug_service.trace(
                        "media_vision_cache_hit",
                        {
                            "media_id": stored_media.id,
                            "content_hash": stored_media.content_hash,
                            "telegram_message_id": stored_media.telegram_message_id,
                        },
                        user_id=message.from_user.id,
                    )
                else:
                    if stored_media.file_bytes is None:
                        payload, _mime_type = media_storage_service.file_payload(stored_media.id)
                        if payload:
                            stored_media = replace(stored_media, file_bytes=payload)
                    description = await asyncio.to_thread(vision_client.describe_image, stored_media)
                    media_storage_service.set_vision_description(stored_media.id, description)
            except Exception:
                logger.exception("vision_description_failed user_id=%s media_id=%s", message.from_user.id, stored_media.id)
                description = "عکس دریافت شده اما توضیح بینایی آن در این لحظه آماده نشد؛ فقط بدان کاربر یک عکس فرستاده است."
                media_storage_service.set_vision_description(stored_media.id, description)
            model_text = image_model_text(description, stored_media.caption, duplicate_context)
            result = await run_chat_turn(message, bot, model_text, profile, reserved_quota_check=quota_check)
        finally:
            await quota_service.finish_generation(message.from_user.id)
        if result:
            await deliver_chat_result(message, bot, result, profile)

    async def answer_unsupported_media(message: Message, text: str) -> None:
        active = await quota_service.active_generation_check(message.from_user.id)
        if active:
            await message.answer(active.message)
            return
        await message.answer(text)

    def record_media_message(message: Message, media: StoredMedia, note: str) -> None:
        history_service.add(
            media.user_id,
            "user",
            f"[{media.media_kind}] {note}",
            chat_id=media.chat_id,
            telegram_message_id=media.telegram_message_id,
            created_at=message.date,
            message_type="media",
            ai_request_payload={
                "media_id": media.id,
                "media_kind": media.media_kind,
                "mime_type": media.mime_type,
                "caption": media.caption,
            },
        )

    @dispatcher.my_chat_member()
    async def my_chat_member_handler(event: ChatMemberUpdated, bot: Bot) -> None:
        if group_service is None or not is_group_chat_type(getattr(event.chat, "type", None)):
            return
        status = getattr(event.new_chat_member.status, "value", str(event.new_chat_member.status))
        group_service.upsert_group(
            chat_id=event.chat.id,
            title=getattr(event.chat, "title", None),
            username=getattr(event.chat, "username", None),
            chat_type=str(event.chat.type),
            bot_status=status,
            member_count=await group_member_count(bot, event.chat.id),
            active=status in {"member", "administrator", "creator"},
        )
        if group_invite_reward_service is None:
            return
        actor = getattr(event, "from_user", None)
        actor_user_id = getattr(actor, "id", None) if actor and not getattr(actor, "is_bot", False) else None
        if status in {"member", "administrator", "creator"}:
            result = group_invite_reward_service.bot_joined_or_promoted(
                chat_id=event.chat.id,
                actor_user_id=actor_user_id,
                status=status,
            )
            if status == "member":
                group_invite_reward_service.bot_removed_or_demoted(chat_id=event.chat.id, status=status)
            user_id = result.get("user_id")
            if user_id and (result.get("member_granted") or result.get("admin_granted")):
                amount = (10 if result.get("member_granted") else 0) + (10 if result.get("admin_granted") else 0)
                with suppress(Exception):
                    await bot.send_message(
                        user_id,
                        f"🎁 نرگس به گروه اضافه شد.\n{amount} پیام رایگان به ظرفیتت اضافه شد.",
                    )
            return
        revoked = group_invite_reward_service.bot_removed_or_demoted(chat_id=event.chat.id, status=status)
        for item in revoked:
            user_id = item.get("user_id")
            if not user_id:
                continue
            amount = (10 if item.get("member_revoked") else 0) + (10 if item.get("admin_revoked") else 0)
            with suppress(Exception):
                await bot.send_message(user_id, f"⚠️ پاداش گروه برگشت خورد.\n{amount} پیام از ظرفیتت کسر شد.")

    async def answer_guest_text(bot: Bot, guest_query_id: str | None, text: str) -> None:
        if not guest_query_id or not text.strip():
            return
        result = InlineQueryResultArticle(
            id=f"narges-guest-{abs(hash((guest_query_id, text))) % 10**12}",
            title="Narges",
            input_message_content=InputTextMessageContent(message_text=text[:4096]),
            description=text[:120],
        )
        await bot.answer_guest_query(guest_query_id=guest_query_id, result=result)

    async def answer_guest_response(bot: Bot, message: Message, guest_query_id: str | None, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if guest_query_id:
            try:
                await answer_guest_text(bot, guest_query_id, text)
                return
            except Exception:
                logger.exception("guest_query_answer_failed chat_id=%s", getattr(message.chat, "id", None))
        with suppress(Exception):
            await bot.send_message(chat_id=message.chat.id, text=text[:4096])

    @dispatcher.update(F.guest_message)
    async def guest_message_handler(update: Update, bot: Bot) -> None:
        message = update.guest_message
        if group_service is None or group_ai_service is None or message is None:
            return
        caller_user = message.guest_bot_caller_user or message.from_user
        if caller_user is None or caller_user.is_bot:
            return
        user_service.upsert_telegram_user(profile_from_user(caller_user))
        schedule_profile_photo_sync(bot, caller_user)
        text = clamp_repeated_chars(group_text(message))
        if meaningful_length(text) < 2:
            return
        guest_query_id = message.guest_query_id
        chat = message.guest_bot_caller_chat or message.chat
        chat_id = getattr(chat, "id", None) or message.chat.id
        message_id = message.message_id or 0
        security_reason = moderation_service.security_warning_reason(text)
        if security_reason:
            warning = moderation_service.apply_model_warning(caller_user.id, security_reason, message_id)
            await answer_guest_response(bot, message, guest_query_id, warning.message)
            return
        if group_service.event_count_since(event_type="guest_response", user_id=caller_user.id, seconds=120) >= 4:
            return
        quota_check = await quota_service.begin_group_generation(caller_user.id)
        if not quota_check.ok:
            return
        try:
            identity = await bot_identity(bot)
            profile = user_service.get(caller_user.id)
            result = await group_ai_service.answer_mention(
                user_id=caller_user.id,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_to_text=compact_reply_context(message),
                message_datetime=message.date,
                user_profile=profile,
                bot_identity=identity,
            )
            quota_service.consume_group_reply(caller_user.id)
            group_service.record_engine_event(
                chat_id=chat_id,
                user_id=caller_user.id,
                event_type="guest_response",
                telegram_message_id=message_id,
                metadata={"provider": result.provider, "model": result.model},
            )
            reply_text = "\n\n".join(item.text for item in result.reply.messages).strip()
            await answer_guest_response(bot, message, guest_query_id, reply_text)
        except Exception:
            logger.exception("guest_message_response_failed chat_id=%s user_id=%s", chat_id, caller_user.id)
            group_service.record_engine_event(
                chat_id=chat_id,
                user_id=caller_user.id,
                event_type="guest_response_failed",
                telegram_message_id=message_id,
            )
        finally:
            await quota_service.finish_generation(caller_user.id)

    @dispatcher.message(F.chat.type.in_({"group", "supergroup"}))
    async def group_message_handler(message: Message, bot: Bot) -> None:
        await track_group_message(message, bot)
        if group_service is None or group_ai_service is None or not message.from_user or message.from_user.is_bot:
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        identity = await bot_identity(bot)
        bot_username = str(identity.get("username") or "@narges_aibot").lstrip("@")
        identity_bot_id = int(identity["bot_id"]) if identity.get("bot_id") is not None else None
        mentioned_narges = is_narges_mentioned(message, bot_username, identity_bot_id)
        reply_to_bot = is_reply_to_bot(message, identity_bot_id)
        triggered = mentioned_narges or reply_to_bot
        standalone_photo = is_standalone_group_photo(message)
        observed_text = group_text(message)
        if observed_text:
            group_service.record_observed_message(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                telegram_message_id=message.message_id,
                text=observed_text,
                created_at=message.date,
                metadata={
                    "content_type": str(message.content_type),
                    "reply_to_message_id": message.reply_to_message.message_id if message.reply_to_message else None,
                    "mentioned_narges": mentioned_narges,
                    "reply_to_bot": reply_to_bot,
                },
            )
        stored_group_media: StoredMedia | None = None
        if message.photo:
            try:
                stored_group_media = await media_storage_service.store_photo(bot, message)
            except MediaStorageError as exc:
                logger.info("group_photo_store_skipped chat_id=%s error=%s", message.chat.id, exc)

        if triggered and is_reasonable_group_mention(message):
            if not await ensure_not_blocked_for_model(message):
                return
            security_reason = moderation_service.security_warning_reason(group_text(message))
            if security_reason:
                warning = moderation_service.apply_model_warning(message.from_user.id, security_reason, message.message_id)
                await message.reply(warning.message, allow_sending_without_reply=True)
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="mention_security_blocked",
                    telegram_message_id=message.message_id,
                    metadata={"reason": security_reason},
                )
                return
            if group_service.event_count_since(event_type="mention_response", chat_id=message.chat.id, seconds=60) >= 8:
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="mention_rate_blocked",
                    telegram_message_id=message.message_id,
                    metadata={"scope": "chat"},
                )
                return
            if group_service.event_count_since(event_type="mention_response", user_id=message.from_user.id, seconds=120) >= 4:
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="mention_rate_blocked",
                    telegram_message_id=message.message_id,
                    metadata={"scope": "user"},
                )
                return
            quota_check = await quota_service.begin_group_generation(message.from_user.id)
            if not quota_check.ok:
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="mention_quota_blocked",
                    telegram_message_id=message.message_id,
                    metadata={"remaining": quota_check.remaining},
                )
                return
            try:
                with suppress(Exception):
                    await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
                profile = user_service.get(message.from_user.id)
                result = await group_ai_service.answer_mention(
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text=group_text(message),
                    reply_to_text=compact_reply_context(message),
                    message_datetime=message.date,
                    user_profile=profile,
                    bot_identity=identity,
                )
                quota_service.consume_group_reply(message.from_user.id)
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="mention_response",
                    telegram_message_id=message.message_id,
                    metadata={"provider": result.provider, "model": result.model},
                )
                for item in result.reply.messages:
                    await asyncio.sleep(min(float(item.delay_seconds or 0), 0.25))
                    sent_message_id = await send_text_with_retries(message, item.text, bot)
                    history_service.set_telegram_message_id(result.assistant_message_id, sent_message_id)
            except Exception:
                logger.exception("group_mention_response_failed chat_id=%s user_id=%s", message.chat.id, message.from_user.id)
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="mention_response_failed",
                    telegram_message_id=message.message_id,
                )
            finally:
                await quota_service.finish_generation(message.from_user.id)
            return

        if standalone_photo:
            photo_cooldown = 5 * 60 * 60
            if group_service.cooldown_active(message.chat.id, "photo_seen", photo_cooldown):
                return
            if group_service.cooldown_active(message.chat.id, "photo_reaction", photo_cooldown):
                return
            if group_service.event_count_since(event_type="photo_reaction", user_id=message.from_user.id, seconds=photo_cooldown) >= 1:
                return
            quota_check = await quota_service.begin_group_generation(message.from_user.id)
            if not quota_check.ok:
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="photo_quota_blocked",
                    telegram_message_id=message.message_id,
                    metadata={"remaining": quota_check.remaining},
                )
                return
            try:
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="photo_seen",
                    telegram_message_id=message.message_id,
                )
                stored_media = stored_group_media or await media_storage_service.store_photo(bot, message)
                matched_bot_image = bot_image_catalog.matching_bot_image(stored_media)
                duplicate_context = media_storage_service.duplicate_context(stored_media)
                if matched_bot_image:
                    description = "کاربر عکس نرگس را ارسال کرده است."
                    media_storage_service.set_vision_description(stored_media.id, description)
                elif cached_description := media_storage_service.cached_vision_description(stored_media):
                    description = cached_description
                    media_storage_service.set_vision_description(stored_media.id, description)
                else:
                    try:
                        description = await asyncio.to_thread(vision_client.describe_image, stored_media)
                    except Exception:
                        logger.exception("group_photo_vision_failed chat_id=%s media_id=%s", message.chat.id, stored_media.id)
                        description = "عکس گروه دریافت شده اما توضیح بینایی آن در این لحظه آماده نشد."
                    media_storage_service.set_vision_description(stored_media.id, description)
                if duplicate_context:
                    description = (
                        f"{description}\n"
                        "نکته: این عکس با hash یکسان قبلا هم به ربات داده شده بود؛ اگر مناسب بود کوتاه به تکراری بودنش اشاره کن."
                    )
                profile = user_service.get(message.from_user.id)
                result = await group_ai_service.answer_photo(
                    user_id=message.from_user.id,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    description=description,
                    message_datetime=message.date,
                    user_profile=profile,
                    bot_identity=identity,
                    media_id=stored_media.id,
                )
                quota_service.consume_group_reply(message.from_user.id)
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="photo_reaction",
                    telegram_message_id=message.message_id,
                    metadata={"media_id": stored_media.id, "provider": result.provider, "model": result.model},
                )
                for item in result.reply.messages[:1]:
                    await asyncio.sleep(min(float(item.delay_seconds or 0), 0.25))
                    sent_message_id = await send_text_with_retries(message, item.text, bot)
                    history_service.set_telegram_message_id(result.assistant_message_id, sent_message_id)
            except Exception:
                logger.exception("group_photo_reaction_failed chat_id=%s user_id=%s", message.chat.id, message.from_user.id)
                group_service.record_engine_event(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    event_type="photo_reaction_failed",
                    telegram_message_id=message.message_id,
                )
            finally:
                await quota_service.finish_generation(message.from_user.id)
            return
        return

    @dispatcher.message(CommandStart())
    async def start_handler(message: Message, bot: Bot) -> None:
        if not message.from_user:
            await message.answer("نتوانستم اطلاعات کاربرت را بخوانم. دوباره /start را بزن.")
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            user_service.set_referred_by_code(message.from_user.id, parts[1].strip())
        if not await ensure_membership(message, bot, use_cache=False):
            return
        profile = user_service.get(message.from_user.id)
        if profile and profile.onboarding_state == OnboardingState.READY:
            await message.answer("سلام ✨\nپیامت رو بفرست؛ نرگس همین‌جاست.", reply_markup=menu_service.reply_menu(can_debug(message.from_user.id)))
            return
        if profile and profile.onboarding_state == OnboardingState.ASK_GENDER:
            await ask_for_gender(message, message.from_user.id)
            return
        await ask_for_name(message)

    @dispatcher.message(Command("new"))
    async def new_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
            schedule_profile_photo_sync(bot, message.from_user)
        if not await ensure_membership(message, bot):
            return
        await message.answer("لازم نیست گفت‌وگوی جدا بسازی ✨\nهر پیام معمولی‌ای بفرستی مستقیم به نرگس می‌رسه.", reply_markup=menu_service.reply_menu(can_debug(message.from_user.id if message.from_user else 0)))

    @dispatcher.message(Command("profile", "account"))
    async def account_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await show_account(message)

    @dispatcher.message(Command("memories", "memory"))
    async def memories_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        if not can_debug(message.from_user.id if message.from_user else 0):
            await message.answer("🧠 بخش حافظه فقط در حالت debug فعاله.")
            return
        await show_memories(message)

    @dispatcher.message(Command("forget"))
    async def forget_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        user_id = message.from_user.id if message.from_user else 0
        if not can_debug(user_id):
            await message.answer("🧠 حذف حافظه فقط در حالت debug فعاله.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("برای حذف حافظه این شکل بفرست: /forget 12")
            return
        deleted = memory_service.delete(user_id, int(parts[1]))
        await message.answer("حذف شد." if deleted else "چنین حافظه‌ای پیدا نشد.")

    @dispatcher.message(Command("settings"))
    async def settings_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await message.answer(
            "اینجا بخش تنظیمات جدا نداریم ✨\nبرای پروفایل و ظرفیت از دکمه‌های پایین استفاده کن.",
            reply_markup=menu_service.reply_menu(can_debug(message.from_user.id if message.from_user else 0)),
        )

    @dispatcher.message(Command("help"))
    async def help_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await message.answer("💬 پیام معمولی بفرست؛ مستقیم به نرگس می‌رسه.\nدکمه‌های پایین هم برای پروفایل و ظرفیت‌اند.", reply_markup=menu_service.reply_menu(can_debug(message.from_user.id if message.from_user else 0)))

    @dispatcher.message(Command("admin_channels"))
    async def admin_channels_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.answer("این دستور فقط برای ادمین است.")
            return
        channels = channel_service.list_all()
        if not channels:
            await message.answer("هنوز کانال اجباری ثبت نشده است.")
            return
        lines = ["کانال‌های اجباری:"]
        for channel in channels:
            state = "فعال" if channel.active else "غیرفعال"
            private = "خصوصی" if channel.is_private else "عمومی"
            lines.append(f"{channel.id}. {channel.title} | {channel.chat_id} | {state} | {private} | pos={channel.position}")
        await message.answer("\n".join(lines))

    @dispatcher.message(Command("admin_add_channel"))
    async def admin_add_channel_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.answer("این دستور فقط برای ادمین است.")
            return
        raw = (message.text or "").split(maxsplit=1)
        if len(raw) != 2:
            await message.answer("فرمت: /admin_add_channel @channel | عنوان | https://t.me/channel | public")
            return
        parts = [part.strip() for part in raw[1].split("|")]
        if len(parts) < 2:
            await message.answer("حداقل chat_id و عنوان لازم است.")
            return
        channel = channel_service.add_channel(
            user_id,
            parts[0],
            parts[1],
            parts[2] if len(parts) >= 3 and parts[2] else None,
            len(parts) >= 4 and parts[3].lower() in {"private", "خصوصی", "1", "true"},
        )
        await message.answer(f"ثبت شد: {channel.id}. {channel.title}")

    @dispatcher.message(Command("admin_remove_channel"))
    async def admin_remove_channel_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.answer("این دستور فقط برای ادمین است.")
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("فرمت: /admin_remove_channel 3")
            return
        await message.answer("حذف شد." if channel_service.remove_channel(user_id, int(parts[1])) else "کانال پیدا نشد.")

    @dispatcher.message(Command("admin_move_channel"))
    async def admin_move_channel_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.answer("این دستور فقط برای ادمین است.")
            return
        parts = (message.text or "").split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("فرمت: /admin_move_channel 3 20")
            return
        await message.answer("مرتب‌سازی ذخیره شد." if channel_service.move_channel(user_id, int(parts[1]), int(parts[2])) else "کانال پیدا نشد.")

    @dispatcher.message(Command("admin_bypass"))
    async def admin_bypass_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.answer("این دستور فقط برای ادمین است.")
            return
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("فرمت: /admin_bypass user_id [minutes] [reason]")
            return
        target_id = int(parts[1])
        minutes = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else settings.admin_bypass_minutes
        channel_service.grant_admin_bypass(user_id, target_id, minutes, parts[3] if len(parts) >= 4 else None)
        await message.answer(f"bypass برای {target_id} به مدت {minutes} دقیقه ثبت شد.")

    @dispatcher.message(Command("admin_help"))
    async def admin_help_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        await send_chunks(
            message,
            "\n".join(
                [
                    "دستورات ادمین:",
                    "/admin_last [limit] [user_id] - آخرین پیام‌ها",
                    "/admin_debug_last - دیباگ آخرین پیام خودت",
                    "/admin_provider [provider] - انتخاب provider پیام‌های بعدی خودت",
                    "/admin_provider_unset - حذف provider اجباری خودت",
                    "/admin_admins - نمایش ادمین‌ها",
                    "/admin_groups [page] - لیست گروه‌ها و آیدی‌ها",
                    "/admin_users [search] [page] - جستجو/لیست کاربران",
                    "/admin_user <user_id> - وضعیت یک کاربر",
                    "/admin_send_user <user_id> <text> - ارسال پیام به کاربر",
                    "/admin_send_group <chat_id> <text> - ارسال پیام به گروه",
                    "/admin_block <user_id> [days] [reason] - مسدود کردن کاربر",
                    "/admin_unblock <user_id> - رفع مسدودی",
                    "/admin_channels - کانال‌های اجباری",
                ]
            ),
        )

    @dispatcher.message(Command("admin_admins"))
    async def admin_admins_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        lines = ["ادمین‌ها:"]
        for admin_id in settings.admin_ids:
            lines.append(f"- `{admin_id}`")
        if settings.debug_user_ids:
            lines.append("debug users:")
            lines.extend(f"- `{user_id}`" for user_id in settings.debug_user_ids)
        await message.answer("\n".join(lines))

    @dispatcher.message(Command("admin_groups"))
    async def admin_groups_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        if group_service is None:
            await message.answer("Group service فعال نیست.")
            return
        args = command_args(message).split()
        page = int(args[0]) if args and args[0].isdigit() else 1
        per_page = 12
        groups = group_service.list_groups(only_active=True)
        start = max(0, page - 1) * per_page
        chunk = groups[start : start + per_page]
        if not chunk:
            await message.answer("گروهی برای نمایش نیست.")
            return
        lines = [f"گروه‌ها - صفحه {page}"]
        for group in chunk:
            lines.append(
                f"`{group.chat_id}` | {group.title or group.username or '-'} | {group.chat_type} | members={group.member_count or '-'} | {group.bot_status or '-'}"
            )
        lines.append(f"کل: {len(groups)}")
        await send_chunks(message, "\n".join(lines))

    @dispatcher.message(Command("admin_users"))
    async def admin_users_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        args = command_args(message).split()
        page = 1
        if args and args[-1].isdigit():
            page = max(1, int(args.pop()))
        query = " ".join(args).strip().lower()
        per_page = 10
        with user_service.database.orm.session() as session:
            rows = session.scalars(select(UserORM).order_by(desc(UserORM.updated_at), desc(UserORM.created_at))).all()
            last_seen_pairs = session.execute(
                select(ConversationMessageORM.user_id, func.max(ConversationMessageORM.created_at)).group_by(ConversationMessageORM.user_id)
            ).all()
        last_seen = {int(user_id): value for user_id, value in last_seen_pairs}
        filtered = []
        for row in rows:
            haystack = " ".join(
                str(value or "")
                for value in [row.telegram_id, row.username, row.first_name, row.last_name, row.display_name, row.phone_number, row.onboarding_state]
            ).lower()
            if query and query not in haystack:
                continue
            filtered.append(row)
        start = (page - 1) * per_page
        chunk = filtered[start : start + per_page]
        if not chunk:
            await message.answer("کاربری پیدا نشد.")
            return
        lines = [f"کاربران - صفحه {page} | نتیجه: {len(filtered)}"]
        for row in chunk:
            block = moderation_service.get_block_status(row.telegram_id)
            blocked = "blocked" if block.blocked else "ok"
            seen = last_seen.get(row.telegram_id)
            seen_text = seen.strftime("%Y-%m-%d") if hasattr(seen, "strftime") else "-"
            name = row.display_name or row.first_name or row.username or "-"
            lines.append(f"`{row.telegram_id}` | {name} | @{row.username or '-'} | {row.onboarding_state} | {blocked} | last={seen_text}")
        await send_chunks(message, "\n".join(lines))

    @dispatcher.message(Command("admin_last"))
    async def admin_last_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        args = command_args(message).split()
        limit = 10
        user_filter: int | None = None
        if args and args[0].isdigit():
            limit = max(1, min(int(args[0]), 30))
        if len(args) >= 2 and args[1].lstrip("-").isdigit():
            user_filter = int(args[1])
        with history_service.database.orm.session() as session:
            statement = select(ConversationMessageORM).order_by(desc(ConversationMessageORM.id)).limit(limit)
            if user_filter is not None:
                statement = statement.where(ConversationMessageORM.user_id == user_filter)
            rows = session.scalars(statement).all()
        if not rows:
            await message.answer("پیامی پیدا نشد.")
            return
        lines = ["آخرین پیام‌ها:"]
        for row in rows:
            text = " ".join((row.text or "").split())[:180]
            lines.append(f"#{row.id} | user={row.user_id} chat={row.chat_id or '-'} | {row.role}/{row.message_type}\n{text}")
        await send_chunks(message, "\n\n".join(lines))

    @dispatcher.message(Command("admin_debug_last"))
    async def admin_debug_last_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        user_id = message.from_user.id if message.from_user else 0
        with history_service.database.orm.session() as session:
            user_row = session.scalar(
                select(ConversationMessageORM)
                .where(ConversationMessageORM.user_id == user_id, ConversationMessageORM.role == "user")
                .order_by(desc(ConversationMessageORM.id))
                .limit(1)
            )
            assistant_rows = []
            if user_row is not None:
                assistant_rows = session.scalars(
                    select(ConversationMessageORM)
                    .where(
                        ConversationMessageORM.user_id == user_id,
                        ConversationMessageORM.role == "assistant",
                        ConversationMessageORM.id > user_row.id,
                    )
                    .order_by(ConversationMessageORM.id.asc())
                    .limit(3)
                ).all()
        if user_row is None:
            await message.answer("پیام قبلی از خودت پیدا نشد.")
            return
        payload = {}
        if assistant_rows and assistant_rows[0].ai_request_payload_json:
            try:
                payload = json.loads(assistant_rows[0].ai_request_payload_json)
            except json.JSONDecodeError:
                payload = {"raw": assistant_rows[0].ai_request_payload_json}
        await send_json(
            message,
            {
                "user_message": {
                    "id": user_row.id,
                    "telegram_message_id": user_row.telegram_message_id,
                    "text": user_row.text,
                    "created_at": user_row.created_at,
                },
                "assistant_messages": [
                    {
                        "id": row.id,
                        "telegram_message_id": row.telegram_message_id,
                        "type": row.message_type,
                        "provider": row.provider,
                        "model": row.model,
                        "tokens": row.total_tokens,
                        "text": row.text,
                    }
                    for row in assistant_rows
                ],
                "assistant_payload": payload,
                "recent_debug_logs": debug_service.recent(10, user_id=user_id),
            },
        )

    @dispatcher.message(Command("admin_provider"))
    async def admin_provider_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        user_id = message.from_user.id if message.from_user else 0
        value = command_args(message).strip()
        choices = chat_service.groq_client.provider_choices() if hasattr(chat_service.groq_client, "provider_choices") else []
        if not value:
            current = (
                chat_service.groq_client.provider_override_for_user(user_id)
                if hasattr(chat_service.groq_client, "provider_override_for_user")
                else None
            )
            await send_chunks(message, "provider فعلی: " + (current or "auto") + "\n\nproviderها:\n" + "\n".join(choices))
            return
        if hasattr(chat_service.groq_client, "set_provider_override"):
            chat_service.groq_client.set_provider_override(user_id, value)
        await message.answer(f"provider پیام‌های بعدی تو تنظیم شد: {value}")

    @dispatcher.message(Command("admin_provider_unset"))
    async def admin_provider_unset_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        user_id = message.from_user.id if message.from_user else 0
        if hasattr(chat_service.groq_client, "clear_provider_override"):
            chat_service.groq_client.clear_provider_override(user_id)
        await message.answer("provider اجباری حذف شد؛ از این به بعد auto است.")

    @dispatcher.message(Command("admin_user"))
    async def admin_user_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        args = command_args(message).split()
        if not args or not args[0].isdigit():
            await message.answer("فرمت: /admin_user user_id")
            return
        target_id = int(args[0])
        profile = user_service.get(target_id)
        if profile is None:
            await message.answer("کاربر پیدا نشد.")
            return
        quota = quota_service.account_quota(target_id)
        block = moderation_service.get_block_status(target_id)
        await send_json(
            message,
            {
                "profile": asdict(profile),
                "quota": quota,
                "block": asdict(block),
                "warnings": moderation_service.warning_count(target_id),
            },
        )

    @dispatcher.message(Command("admin_send_user"))
    async def admin_send_user_handler(message: Message, bot: Bot) -> None:
        if not await require_admin_command(message):
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await message.answer("فرمت: /admin_send_user user_id متن پیام")
            return
        target_id = int(parts[1])
        text = parts[2].strip()
        try:
            sent = await bot.send_message(target_id, text)
        except Exception as exc:
            await message.answer(f"ارسال ناموفق: {exc.__class__.__name__}: {exc}")
            return
        history_service.add(target_id, "assistant", text, chat_id=target_id, telegram_message_id=sent.message_id, message_type="admin_direct")
        await message.answer(f"ارسال شد به {target_id}. message_id={sent.message_id}")

    @dispatcher.message(Command("admin_send_group"))
    async def admin_send_group_handler(message: Message, bot: Bot) -> None:
        if not await require_admin_command(message):
            return
        if group_service is None:
            await message.answer("Group service فعال نیست.")
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
            await message.answer("فرمت: /admin_send_group chat_id متن پیام")
            return
        chat_id = int(parts[1])
        text = parts[2].strip()
        try:
            sent = await bot.send_message(chat_id, text)
        except Exception as exc:
            await message.answer(f"ارسال ناموفق: {exc.__class__.__name__}: {exc}")
            return
        group_service.record_outbound_message(chat_id=chat_id, text=text, message_type="group_admin", telegram_message_id=sent.message_id)
        group_service.record_engine_event(chat_id=chat_id, event_type="admin_group_message", telegram_message_id=sent.message_id, metadata={"source": "telegram_admin_command"})
        await message.answer(f"ارسال شد به گروه {chat_id}. message_id={sent.message_id}")

    @dispatcher.message(Command("admin_block"))
    async def admin_block_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        parts = (message.text or "").split(maxsplit=3)
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("فرمت: /admin_block user_id [days] [reason]")
            return
        target_id = int(parts[1])
        days = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 7
        reason = parts[3] if len(parts) >= 4 else "manual admin block"
        status = moderation_service.block_user(target_id, days, reason)
        await message.answer(f"کاربر {target_id} تا {status.blocked_until} مسدود شد.")

    @dispatcher.message(Command("admin_unblock"))
    async def admin_unblock_handler(message: Message) -> None:
        if not await require_admin_command(message):
            return
        args = command_args(message).split()
        if not args or not args[0].isdigit():
            await message.answer("فرمت: /admin_unblock user_id")
            return
        target_id = int(args[0])
        ok = moderation_service.unblock_user(target_id)
        await message.answer("رفع مسدودی انجام شد." if ok else "این کاربر مسدودی فعال نداشت.")

    @dispatcher.message(Command("debug_account"))
    async def debug_account_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not debug_service.can_debug(user_id):
            return
        profile = user_service.get(user_id)
        await send_json(message, {"profile": asdict(profile) if profile else None, "quota": quota_service.account_quota(user_id)})

    @dispatcher.message(Command("debug_memories"))
    async def debug_memories_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not debug_service.can_debug(user_id):
            return
        await send_json(message, {"memories": [item.model_dump(mode="json") for item in memory_service.list_active(user_id, 100)]})

    @dispatcher.message(Command("debug_quota"))
    async def debug_quota_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not debug_service.can_debug(user_id):
            return
        await send_json(message, {"quota": quota_service.account_quota(user_id)})

    @dispatcher.message(Command("debug_state"))
    async def debug_state_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not debug_service.can_debug(user_id):
            return
        await send_json(message, {"narges_state": narges_state_service.get_active()})

    @dispatcher.message(Command("debug_logs"))
    async def debug_logs_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not debug_service.can_debug(user_id):
            return
        await send_json(message, {"logs": debug_service.recent(30)})

    @dispatcher.message(Command("debug_all"))
    async def debug_all_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not debug_service.can_debug(user_id):
            return
        profile = user_service.get(user_id)
        await send_json(
            message,
            {
                "profile": profile,
                "quota": quota_service.account_quota(user_id),
                "block": moderation_service.get_block_status(user_id),
                "narges_state": narges_state_service.get_active(),
                "memories": memory_service.list_active(user_id, 100),
                "billing_invoices": billing_service.list_user_invoices(user_id, 50),
                "recent_history": history_service.recent_turns(user_id, limit=5),
                "debug_logs": debug_service.recent(30, user_id=user_id),
            },
        )

    @dispatcher.callback_query(F.data == "onboarding:check_channels")
    async def check_channels_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message or not callback.from_user:
            return
        user_service.upsert_telegram_user(profile_from_user(callback.from_user))
        check = await channel_service.check_user(bot, callback.from_user.id, use_cache=False)
        if not check.ok:
            await callback.answer("هنوز عضویت کامل نیست.", show_alert=False)
            profile = user_service.get(callback.from_user.id)
            await show_membership_gate(
                callback.message,
                bot,
                check,
                verification_failed=bool(profile and profile.membership_state == "required"),
                user_id=callback.from_user.id,
            )
            return
        await callback.answer("عضویت تأیید شد.")
        await delete_membership_gate(bot, callback.from_user.id)
        user_service.mark_membership_ok(callback.from_user.id)
        profile = user_service.recover_registration_state(callback.from_user.id)
        if profile and profile.onboarding_state == OnboardingState.READY:
            await callback.message.answer("عضویت تأیید شد ✨\nحالا پیامت رو بفرست.", reply_markup=menu_service.reply_menu(can_debug(callback.from_user.id)))
            return
        if profile and profile.onboarding_state == OnboardingState.ASK_GENDER:
            await ask_for_gender(callback.message, callback.from_user.id)
            return
        await ask_for_name(callback.message)

    @dispatcher.callback_query(F.data == "onboarding:name_confirm")
    async def confirm_name_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        if not profile or not profile.suggested_name:
            user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
            await callback.message.answer("🌸 چی صدات کنم؟\nاسمت رو کوتاه بنویس.")
            return
        await callback.answer("ثبت شد.")
        with suppress(Exception):
            await callback.message.delete()
        await finish_name_update(callback.message, callback.from_user.id, profile.suggested_name, bot)

    @dispatcher.callback_query(F.data == "onboarding:name_change")
    async def change_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
        await callback.answer()
        with suppress(Exception):
            await callback.message.delete()
        await callback.message.answer("باشه ✨\nاسم دلخواهت رو کوتاه بنویس.")

    @dispatcher.callback_query(F.data == "onboarding:name_retry")
    async def retry_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
        await callback.answer()
        await callback.message.answer("اسم فارسی رو دوباره بنویس؛ مثلا: نرگس")

    @dispatcher.callback_query(F.data == "onboarding:name_cancel")
    async def cancel_name_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        await callback.answer("لغو شد.")
        if profile and profile.registration_state == OnboardingState.READY:
            user_service.set_onboarding_state(callback.from_user.id, OnboardingState.READY)
            await delete_prompt_message(bot, callback.from_user.id)
            await callback.message.answer("لغو شد. اسم قبلی دست‌نخورده ماند.", reply_markup=menu_service.reply_menu(can_debug(callback.from_user.id)))
            return
        await ask_for_name(callback.message)

    @dispatcher.callback_query(F.data == "onboarding:ambiguous_confirm")
    async def ambiguous_name_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        if profile and profile.pending_name:
            with suppress(Exception):
                await callback.message.delete()
            await finish_name_update(callback.message, callback.from_user.id, profile.pending_name, bot)
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith("onboarding:gender:"))
    async def gender_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        gender = (callback.data or "").split(":", 2)[2]
        gender = gender if gender in {"female", "male", "unspecified"} else "unspecified"
        await callback.answer("ثبت شد.")
        with suppress(Exception):
            await callback.message.delete()
        await finish_gender_update(callback.message, callback.from_user.id, None if gender == "unspecified" else gender, bot)

    @dispatcher.callback_query(F.data.startswith("menu:"))
    async def menu_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        user_service.upsert_telegram_user(profile_from_user(callback.from_user))
        check = await channel_service.check_user(bot, callback.from_user.id)
        if not check.ok:
            await callback.answer("اول عضویت کانال‌ها را کامل کن.")
            await show_membership_gate(callback.message, bot, check, user_id=callback.from_user.id)
            return
        action = (callback.data or "").split(":", 1)[1]
        await callback.answer()
        if action == "memories":
            await show_memories(callback.message)
        elif action in {"profile", "account"}:
            await show_account(callback.message)
        elif action == "referral":
            await show_referral(callback.message, bot)
        elif action == "help":
            await callback.message.answer("💬 هر پیام معمولی‌ای بفرستی مستقیم به نرگس می‌رسه.\nاز دکمه‌های پایین هم برای پروفایل و ظرفیت استفاده کن.")

    @dispatcher.callback_query(F.data == "account:edit_name")
    async def account_edit_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_service.set_onboarding_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
        await callback.answer()
        sent = await callback.message.answer("✏️ اسم جدیدی که می‌خوای باهاش صدات کنم رو بفرست.")
        user_service.set_prompt_message(callback.from_user.id, sent.chat.id, sent.message_id)

    @dispatcher.callback_query(F.data == "account:edit_gender")
    async def account_edit_gender_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        await ask_for_gender(callback.message, callback.from_user.id)

    @dispatcher.callback_query(F.data == "capacity:open")
    async def capacity_open_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        profile = user_service.get(callback.from_user.id)
        await callback.message.answer(
            capacity_text(profile),
            reply_markup=menu_service.capacity_keyboard(phone_bonus_available(profile)),
        )

    @dispatcher.callback_query(F.data == "billing:stars_menu")
    async def billing_stars_menu_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        await callback.message.answer(
            "خرید با Stars\n\nپلن موردنظرت را انتخاب کن. ظرفیت فقط بعد از پرداخت موفق فعال می‌شود.",
            reply_markup=menu_service.stars_plans_keyboard(),
        )

    @dispatcher.callback_query(F.data == "billing:card_menu")
    async def billing_card_menu_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        await callback.message.answer(
            "💳 خرید پیام\n\nهر ۱۰۰ پیام ۱۵۰ هزار تومان است. بسته آخر تخفیف دارد.\nپلن را انتخاب کن:",
            reply_markup=menu_service.card_plans_keyboard(),
        )

    @dispatcher.callback_query(F.data == "billing:back")
    async def billing_back_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        profile = user_service.get(callback.from_user.id)
        await callback.message.edit_text(
            capacity_text(profile),
            reply_markup=menu_service.capacity_keyboard(phone_bonus_available(profile)),
        )

    @dispatcher.callback_query(F.data.startswith("billing:plan:"))
    async def billing_plan_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        user_id = callback.from_user.id
        plan_id = (callback.data or "").removeprefix("billing:card_plan:")
        plan = billing_service.get_plan(plan_id)
        if plan is None:
            await callback.answer("این پلن معتبر نیست.", show_alert=True)
            return
        invoice = billing_service.create_invoice(user_id, plan.id)
        payload = billing_service.payload_for_invoice(invoice)
        await callback.answer()
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=f"{plan.message_quota} پیام نرگس",
            description=f"افزایش ظرفیت حساب با {plan.message_quota} پیام. فعال‌سازی فقط بعد از پرداخت موفق Stars انجام می‌شود.",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=plan.title, amount=plan.stars_cost)],
        )

    @dispatcher.callback_query(F.data.startswith("billing:card_plan:"))
    async def billing_card_plan_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_id = callback.from_user.id
        plan_id = (callback.data or "").split(":", 2)[2]
        plan = billing_service.get_plan(plan_id)
        if plan is None or plan.payment_method != "card":
            await callback.answer("این پلن معتبر نیست.", show_alert=True)
            return
        invoice = billing_service.create_card_invoice(user_id, plan.id)
        await callback.answer()
        await callback.message.answer(card_payment_text(invoice))

    @dispatcher.message(F.photo)
    async def photo_handler(message: Message, bot: Bot) -> None:
        if not message.from_user or not message.photo:
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        if not await ensure_membership(message, bot):
            return
        if not await ensure_not_blocked_for_model(message):
            return
        try:
            stored_media = await media_storage_service.store_photo(bot, message)
        except MediaStorageError as exc:
            await message.answer(media_storage_error_message(exc))
            return
        pending = billing_service.latest_pending_card_invoice(message.from_user.id)
        if pending:
            invoice = billing_service.attach_card_receipt(message.from_user.id, f"photo:{stored_media.id}:{stored_media.telegram_file_id}")
            if invoice:
                await message.answer(
                    "🧾 رسیدت ثبت شد.\n\n"
                    "منتظر بررسی ادمین بمان. نتیجه همینجا اعلام می‌شود."
                )
            return
        profile = user_service.get(message.from_user.id)
        if profile is None or profile.onboarding_state != OnboardingState.READY:
            await ask_for_name(message)
            return
        await handle_ready_image(message, bot, stored_media, profile)

    @dispatcher.message(F.document)
    async def document_handler(message: Message, bot: Bot) -> None:
        if not message.from_user or not message.document:
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        if not await ensure_membership(message, bot):
            return
        if not await ensure_not_blocked_for_model(message):
            return
        mime_type = (message.document.mime_type or mimetypes.guess_type(message.document.file_name or "")[0] or "").lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            await answer_unsupported_media(message, UNSUPPORTED_MEDIA_MESSAGE)
            return
        try:
            stored_media = await media_storage_service.store_document(bot, message)
        except MediaStorageError as exc:
            await message.answer(media_storage_error_message(exc))
            return
        pending = billing_service.latest_pending_card_invoice(message.from_user.id)
        if pending:
            invoice = billing_service.attach_card_receipt(message.from_user.id, f"document:{stored_media.id}:{stored_media.telegram_file_id}")
            if invoice:
                await message.answer(
                    "🧾 رسیدت ثبت شد.\n\n"
                    "منتظر بررسی ادمین بمان. نتیجه همینجا اعلام می‌شود."
                )
            return
        profile = user_service.get(message.from_user.id)
        if profile is None or profile.onboarding_state != OnboardingState.READY:
            await ask_for_name(message)
            return
        if media_storage_service.is_supported_image(stored_media):
            await handle_ready_image(message, bot, stored_media, profile)
            return
        record_media_message(message, stored_media, "unsupported document")
        await answer_unsupported_media(message, UNSUPPORTED_MEDIA_MESSAGE)

    @dispatcher.message(F.voice | F.video | F.audio | F.animation | F.video_note | F.sticker)
    async def unsupported_media_handler(message: Message, bot: Bot) -> None:
        if not message.from_user:
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        if not await ensure_membership(message, bot):
            return
        if not await ensure_not_blocked_for_model(message):
            return
        text = UNSUPPORTED_AUDIO_MESSAGE if (message.voice or message.audio or message.video or message.video_note) else UNSUPPORTED_MEDIA_MESSAGE
        if message.voice:
            try:
                stored_media = await media_storage_service.store_unsupported_media(bot, message)
            except MediaStorageError as exc:
                await message.answer(media_storage_error_message(exc))
                return
            record_media_message(message, stored_media, "unsupported voice")
        else:
            history_service.add(
                message.from_user.id,
                "user",
                "[unsupported_media] not stored",
                chat_id=message.chat.id,
                telegram_message_id=message.message_id,
                created_at=message.date,
                message_type="media",
                ai_request_payload={
                    "source": "unsupported_media",
                    "stored": False,
                    "reason": "only images and voice are stored",
                    "content_type": message.content_type,
                },
            )
        await answer_unsupported_media(message, text)

    @dispatcher.callback_query(F.data == "capacity:phone")
    async def capacity_phone_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        if not phone_bonus_available(profile):
            await callback.answer("شماره موبایل قبلاً ثبت شده.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            "برای دریافت ۲۰ پیام اضافه، باید شماره موبایل همان اکانت تلگرام را با دکمه زیر بفرستی.",
            reply_markup=menu_service.phone_request_keyboard(),
        )

    @dispatcher.callback_query(F.data == "capacity:groups")
    async def capacity_groups_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        await callback.answer()
        try:
            me = await bot.get_me()
            username = me.username
        except Exception:
            username = "narges_aibot"
        await callback.message.answer(
            menu_service.group_invite_text(),
            reply_markup=menu_service.group_invite_keyboard(username),
        )

    @dispatcher.callback_query(F.data == "capacity:referral")
    async def capacity_referral_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        await callback.answer()
        with suppress(Exception):
            await callback.message.delete()
        await show_referral(callback.message, bot)

    @dispatcher.pre_checkout_query()
    async def pre_checkout_handler(query: PreCheckoutQuery) -> None:
        if billing_service.can_checkout(query.from_user.id, query.invoice_payload):
            await query.answer(ok=True)
            return
        await query.answer(ok=False, error_message="فاکتور معتبر نیست یا قبلاً پردازش شده است.")

    @dispatcher.message(F.successful_payment)
    async def successful_payment_handler(message: Message) -> None:
        if not message.from_user or not message.successful_payment:
            return
        payment = message.successful_payment
        payment_id = payment.telegram_payment_charge_id or payment.provider_payment_charge_id
        result = billing_service.confirm_successful_stars_payment(
            user_id=message.from_user.id,
            payload=payment.invoice_payload,
            payment_id=payment_id,
        )
        if not result.accepted or result.invoice is None:
            logger.warning("stars_payment_rejected user_id=%s reason=%s", message.from_user.id, result.reason)
            await message.answer("پرداخت دریافت شد ولی فاکتور داخلی معتبر نبود. پشتیبانی بررسی می‌کند.")
            return
        if result.newly_paid:
            quota_service.add_extra_credit(
                message.from_user.id,
                result.invoice.message_quota,
                reason=f"stars:{result.invoice.invoice_id}",
            )
            await message.answer(f"پرداخت موفق بود. {result.invoice.message_quota} پیام به ظرفیت اضافه‌ات اضافه شد.")
            return
        await message.answer("این پرداخت قبلاً ثبت شده و ظرفیتش اضافه شده بود.")

    @dispatcher.message(F.contact)
    async def contact_handler(message: Message, bot: Bot) -> None:
        if not message.from_user or not message.contact:
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        if not await ensure_membership(message, bot):
            return
        if not await ensure_not_blocked_for_model(message):
            return
        if message.contact.user_id != message.from_user.id:
            await message.answer(
                "این شماره متعلق به اکانت تلگرام تو نیست. فقط با دکمه ارسال شماره موبایل اکانت خودت قابل قبول است.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        can_claim = user_service.save_phone_number(message.from_user.id, message.contact.phone_number)
        if not can_claim:
            await message.answer("شماره‌ات قبلاً ثبت شده و پاداش ۲۰ پیام را گرفته‌ای.", reply_markup=ReplyKeyboardRemove())
            return
        quota_service.add_extra_credit(message.from_user.id, 20, reason="phone")
        user_service.mark_phone_bonus_claimed(message.from_user.id)
        await message.answer("شماره موبایل تأیید شد. ۲۰ پیام به ظرفیت اضافه‌ات اضافه شد.", reply_markup=ReplyKeyboardRemove())
        await show_account(message)

    @dispatcher.message(F.text)
    async def message_handler(message: Message, bot: Bot) -> None:
        if not message.from_user:
            await message.answer("نتوانستم اطلاعات کاربرت را بخوانم.")
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        schedule_profile_photo_sync(bot, message.from_user)
        if await handle_name_text(message, bot):
            return
        if not await ensure_membership(message, bot):
            return
        profile = user_service.get(message.from_user.id)
        text_value = clamp_repeated_chars((message.text or "").strip())
        if profile and profile.onboarding_state == OnboardingState.ASK_GENDER:
            await ask_for_gender(message, message.from_user.id)
            return
        if profile is None or profile.onboarding_state != OnboardingState.READY:
            await ask_for_name(message)
            return
        if not await ensure_not_blocked_for_model(message):
            return
        pending_card_invoice = billing_service.latest_pending_card_invoice(message.from_user.id)
        if pending_card_invoice and looks_like_receipt_message(text_value):
            invoice = billing_service.attach_card_receipt(message.from_user.id, text_value)
            if invoice:
                await message.answer(
                    "🧾 رسیدت ثبت شد.\n\n"
                    "منتظر بررسی ادمین بمان. نتیجه همینجا اعلام می‌شود."
                )
            return
        if text_value == "👤 پروفایل":
            await show_account(message)
            return
        if text_value == "🎁 دعوت دوستان":
            await show_referral(message, bot)
            return
        if text_value == "⚡ افزایش ظرفیت":
            profile = user_service.get(message.from_user.id)
            await message.answer(
                capacity_text(profile),
                reply_markup=menu_service.capacity_keyboard(phone_bonus_available(profile)),
            )
            return
        if text_value == "💬 راهنما":
            await message.answer("💬 پیام معمولی بفرست؛ مستقیم به نرگس می‌رسه.\nبرای حساب و ظرفیت هم از دکمه‌های پایین استفاده کن.")
            return
        if text_value == "🧠 حافظه‌ها":
            if not can_debug(message.from_user.id):
                await message.answer("🧠 بخش حافظه فقط در حالت debug فعاله.")
                return
            await show_memories(message)
            return
        if meaningful_length(text_value) < 2:
            await message.answer("متوجه نشدمم دوباره بگو")
            return
        result = await run_chat_turn(message, bot, text_value, profile)
        if result:
            await deliver_chat_result(message, bot, result, profile)
