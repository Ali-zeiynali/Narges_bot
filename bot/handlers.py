import asyncio
import json
import logging
from dataclasses import asdict, is_dataclass
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery, ReplyKeyboardRemove, User
from pydantic import BaseModel

from bot.config import Settings
from bot.models.channel import MembershipCheck
from bot.models.user import OnboardingState, TelegramUserProfile
from bot.services.billing_service import BillingService
from bot.services.chat_service import ChatService, UserFacingError
from bot.services.debug_service import DebugService
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.services.menu_service import MenuService
from bot.services.moderation_service import ModerationService
from bot.services.narges_state_service import NargesStateService
from bot.services.name_service import NameService
from bot.services.quota_service import QuotaService
from bot.services.required_channel_service import RequiredChannelService
from bot.services.user_service import UserService


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

    async def show_membership_gate(message: Message, check: MembershipCheck) -> None:
        text = (
            "فعلاً نتوانستم عضویت کانال‌ها را از تلگرام بررسی کنم.\n"
            "اگر عضو شده‌ای، چند لحظه بعد دوباره بررسی کن."
            if check.errors
            else "برای استفاده از نرگس باید اول عضو کانال‌های زیر باشی.\nبعد از عضویت، دکمه بررسی را بزن."
        )
        await message.answer(text, reply_markup=menu_service.membership_keyboard(check))

    async def ensure_membership(message: Message, bot: Bot, use_cache: bool = True) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        check = await channel_service.check_user(bot, user_id, use_cache=use_cache)
        if check.ok:
            return True
        user_service.set_state(user_id, OnboardingState.NEED_CHANNELS)
        await show_membership_gate(message, check)
        return False

    async def ensure_not_blocked_for_model(message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        if is_admin(user_id):
            return True
        status = moderation_service.get_block_status(user_id)
        if not status.blocked:
            return True
        await message.answer(moderation_service.block_message(status))
        return False

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

    async def finish_name_update(message: Message, user_id: int, name: str) -> None:
        user_service.save_display_name(user_id, name)
        memory_service.upsert_identity_name(user_id, name, message.message_id)
        await ask_for_gender(message, user_id, name)

    async def finish_gender_update(message: Message, user_id: int, gender: str | None) -> None:
        user_service.save_gender(user_id, gender)
        await message.answer(
            "تمومه ✨\nاز الان هرچی بفرستی مستقیم می‌رسه به نرگس.",
            reply_markup=menu_service.reply_menu(can_debug(user_id)),
        )

    async def handle_name_text(message: Message) -> bool:
        if not message.from_user:
            return False
        profile = user_service.get(message.from_user.id)
        if profile is None or profile.onboarding_state not in {
            OnboardingState.ASK_NAME_INPUT,
            OnboardingState.NAME_AMBIGUOUS_CONFIRM,
        }:
            return False

        if is_same_name_request(message.text or "") and (profile.pending_name or profile.suggested_name):
            await finish_name_update(message, message.from_user.id, profile.pending_name or profile.suggested_name)  # type: ignore[arg-type]
            return True

        result = name_service.validate(message.text or "", allow_ambiguous=True)
        if not result.ok or not result.normalized:
            await message.answer(
                f"اسم رو نتونستم درست ذخیره کنم 🌙\n{result.reason or ''}\n\nیه اسم کوتاه و واقعی بنویس؛ مثلا: علی"
            )
            return True
        if result.ambiguous and not profile.name_confirm_attempted:
            user_service.set_pending_name(message.from_user.id, result.normalized, attempted=True)
            user_service.set_state(message.from_user.id, OnboardingState.NAME_AMBIGUOUS_CONFIRM)
            await message.answer(
                f"{result.normalized} را همین‌طوری ذخیره کنم؟",
                reply_markup=menu_service.ambiguous_name_confirm(),
            )
            return True
        await finish_name_update(message, message.from_user.id, result.normalized)
        return True

    async def show_memories(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not can_debug(user_id):
            await message.answer("🧠 بخش حافظه فقط در حالت debug فعاله.")
            return
        memories = memory_service.list_active(user_id, limit=30)
        if not memories:
            await message.answer("هنوز چیزی برایت ذخیره نکرده‌ام.")
            return
        lines = ["حافظه‌های فعال:"]
        lines.extend(f"{item.id}. {item.kind.value}: {item.summary}" for item in memories)
        await message.answer("\n".join(lines))

    async def show_account(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        profile = user_service.get(user_id)
        quota = quota_service.account_quota(user_id)
        block = moderation_service.get_block_status(user_id)
        if profile is None:
            await message.answer("اول /start را بزن تا حساب کاربری‌ات ساخته شود.")
            return
        block_line = "فعال نیست"
        if block.blocked and block.blocked_until:
            block_line = f"مسدود تا {block.blocked_until.strftime('%Y-%m-%d %H:%M UTC')}"
        await message.answer(
            "👤 پروفایل\n\n"
            f"نام: {profile.display_name or 'ثبت نشده'}\n"
            f"جنسیت: {gender_label(profile.gender)}\n"
            f"مصرف کل: {format_quota_units(quota.total_sent)} پیام\n"
            f"باقی‌مانده روزانه: {format_quota_units(quota.daily_remaining)} از {quota.daily_limit}\n"
            f"باقی‌مانده ماهانه: {format_quota_units(quota.monthly_remaining)} از {quota.monthly_limit}\n"
            f"ظرفیت اضافه: {format_quota_units(quota.extra_remaining)} پیام\n"
            f"شماره موبایل: {'✅ ثبت شده' if profile.phone_number else 'ثبت نشده'}\n"
            f"وضعیت مسدودی: {block_line}",
            reply_markup=menu_service.account_keyboard(can_debug(user_id)),
        )

    @dispatcher.message(CommandStart())
    async def start_handler(message: Message, bot: Bot) -> None:
        if not message.from_user:
            await message.answer("نتوانستم اطلاعات کاربرت را بخوانم. دوباره /start را بزن.")
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
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
            await show_membership_gate(callback.message, check)
            return
        await callback.answer("عضویت تأیید شد.")
        profile = user_service.get(callback.from_user.id)
        if profile and profile.onboarding_state == OnboardingState.READY:
            await callback.message.answer("عضویت تأیید شد ✨\nحالا پیامت رو بفرست.", reply_markup=menu_service.reply_menu(can_debug(callback.from_user.id)))
            return
        if profile and profile.onboarding_state == OnboardingState.ASK_GENDER:
            await ask_for_gender(callback.message, callback.from_user.id)
            return
        await ask_for_name(callback.message)

    @dispatcher.callback_query(F.data == "onboarding:name_confirm")
    async def confirm_name_callback(callback: CallbackQuery) -> None:
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
        await finish_name_update(callback.message, callback.from_user.id, profile.suggested_name)

    @dispatcher.callback_query(F.data == "onboarding:name_change")
    async def change_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
        await callback.answer()
        with suppress(Exception):
            await callback.message.delete()
        await callback.message.answer("باشه ✨\nاسم دلخواهت رو کوتاه بنویس.")

    @dispatcher.callback_query(F.data == "onboarding:ambiguous_confirm")
    async def ambiguous_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        if profile and profile.pending_name:
            with suppress(Exception):
                await callback.message.delete()
            await finish_name_update(callback.message, callback.from_user.id, profile.pending_name)
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith("onboarding:gender:"))
    async def gender_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        gender = (callback.data or "").split(":", 2)[2]
        gender = gender if gender in {"female", "male", "unspecified"} else "unspecified"
        await callback.answer("ثبت شد.")
        with suppress(Exception):
            await callback.message.delete()
        await finish_gender_update(callback.message, callback.from_user.id, None if gender == "unspecified" else gender)

    @dispatcher.callback_query(F.data.startswith("menu:"))
    async def menu_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        user_service.upsert_telegram_user(profile_from_user(callback.from_user))
        check = await channel_service.check_user(bot, callback.from_user.id)
        if not check.ok:
            await callback.answer("اول عضویت کانال‌ها را کامل کن.")
            await show_membership_gate(callback.message, check)
            return
        action = (callback.data or "").split(":", 1)[1]
        await callback.answer()
        if action == "memories":
            await show_memories(callback.message)
        elif action in {"profile", "account"}:
            await show_account(callback.message)
        elif action == "help":
            await callback.message.answer("💬 هر پیام معمولی‌ای بفرستی مستقیم به نرگس می‌رسه.\nاز دکمه‌های پایین هم برای پروفایل و ظرفیت استفاده کن.")

    @dispatcher.callback_query(F.data == "account:edit_name")
    async def account_edit_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
        await callback.answer()
        await callback.message.answer("✏️ اسم جدیدی که می‌خوای باهاش صدات کنم رو بفرست.")

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
        await callback.message.answer(
            "افزایش ظرفیت\n\nمی‌تونی با Stars ظرفیت اضافه کنی یا یک‌بار شماره موبایل اکانت تلگرامت را برای ۳۰ پیام اضافه بفرستی.",
            reply_markup=menu_service.capacity_keyboard(),
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

    @dispatcher.callback_query(F.data == "billing:back")
    async def billing_back_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        await callback.message.edit_text(
            "افزایش ظرفیت\n\nمی‌تونی با Stars ظرفیت اضافه کنی یا شماره موبایل اکانت تلگرامت را بفرستی.",
            reply_markup=menu_service.capacity_keyboard(),
        )

    @dispatcher.callback_query(F.data.startswith("billing:plan:"))
    async def billing_plan_callback(callback: CallbackQuery, bot: Bot) -> None:
        if not callback.message:
            return
        user_id = callback.from_user.id
        plan_id = (callback.data or "").split(":", 2)[2]
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

    @dispatcher.callback_query(F.data == "capacity:phone")
    async def capacity_phone_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        await callback.answer()
        await callback.message.answer(
            "برای دریافت ۳۰ پیام اضافه، باید شماره موبایل همان اکانت تلگرام را با دکمه زیر بفرستی.",
            reply_markup=menu_service.phone_request_keyboard(),
        )

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
        if not await ensure_membership(message, bot):
            return
        if message.contact.user_id != message.from_user.id:
            await message.answer(
                "این شماره متعلق به اکانت تلگرام تو نیست. فقط با دکمه ارسال شماره موبایل اکانت خودت قابل قبول است.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        can_claim = user_service.save_phone_number(message.from_user.id, message.contact.phone_number)
        if not can_claim:
            await message.answer("شماره‌ات قبلاً ثبت شده و پاداش ۳۰ پیام را گرفته‌ای.", reply_markup=ReplyKeyboardRemove())
            return
        quota_service.add_extra_credit(message.from_user.id, 30, reason="phone")
        user_service.mark_phone_bonus_claimed(message.from_user.id)
        await message.answer("شماره موبایل تأیید شد. ۳۰ پیام به ظرفیت اضافه‌ات اضافه شد.", reply_markup=ReplyKeyboardRemove())
        await show_account(message)

    @dispatcher.message(F.text)
    async def message_handler(message: Message, bot: Bot) -> None:
        if not message.from_user:
            await message.answer("نتوانستم اطلاعات کاربرت را بخوانم.")
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if await handle_name_text(message):
            return
        if not await ensure_membership(message, bot):
            return
        profile = user_service.get(message.from_user.id)
        text_value = (message.text or "").strip()
        if profile and profile.onboarding_state == OnboardingState.ASK_GENDER:
            await ask_for_gender(message, message.from_user.id)
            return
        if profile is None or profile.onboarding_state != OnboardingState.READY:
            await ask_for_name(message)
            return
        if text_value == "👤 پروفایل":
            await show_account(message)
            return
        if text_value == "⚡ افزایش ظرفیت":
            await message.answer(
                "⚡ افزایش ظرفیت\n\nمی‌تونی با Stars ظرفیت اضافه بگیری یا یک‌بار شماره همین اکانت تلگرام رو بفرستی.",
                reply_markup=menu_service.capacity_keyboard(),
            )
            return
        if text_value == "💬 راهنما":
            await message.answer("💬 پیام معمولی بفرست؛ مستقیم به نرگس می‌رسه.\nبرای حساب و ظرفیت هم از دکمه‌های پایین استفاده کن.")
            return
        if text_value == "🧠 حافظه‌ها":
            await show_memories(message)
            return
        if not await ensure_not_blocked_for_model(message):
            return

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(keep_typing(bot, message.chat.id, stop_typing))
        try:
            result = await chat_service.answer(
                message.from_user.id,
                message.chat.id,
                message.message_id,
                text_value,
                message.date,
                user_profile=profile,
            )
        except UserFacingError as exc:
            text = str(exc)
            stop_typing.set()
            with suppress(asyncio.CancelledError):
                await typing_task
            if any(word in text for word in ("سهمیه", "ظرفیت", "تند", "سقف", "limit")):
                await message.answer(text, reply_markup=menu_service.capacity_keyboard())
            else:
                await message.answer(text)
            return
        finally:
            stop_typing.set()
            with suppress(asyncio.CancelledError):
                await typing_task

        logger.info(
            "answer_ready user_id=%s chat_id=%s estimated_tokens=%s total_tokens=%s",
            message.from_user.id,
            message.chat.id,
            result.estimated_tokens,
            result.usage.get("total_tokens"),
        )
        for item in result.reply.messages:
            await asyncio.sleep(item.delay_seconds)
            await message.answer(item.text)

