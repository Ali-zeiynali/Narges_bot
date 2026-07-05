import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message, User

from bot.config import Settings
from bot.models.channel import MembershipCheck
from bot.models.user import OnboardingState, TelegramUserProfile
from bot.services.chat_service import ChatService, UserFacingError
from bot.services.memory_service import MemoryService
from bot.services.menu_service import MenuService
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

    async def show_membership_gate(message: Message, check: MembershipCheck) -> None:
        if check.errors:
            await message.answer(
                "فعلاً نتوانستم عضویت کانال‌ها را از تلگرام بررسی کنم.\n"
                "اگر عضو شده‌ای، چند لحظه بعد دوباره بررسی کن.",
                reply_markup=menu_service.membership_keyboard(check),
            )
            return
        await message.answer(
            "برای استفاده از نرگس باید اول عضو کانال‌های زیر باشی.\n"
            "بعد از عضویت، دکمه بررسی را بزن.",
            reply_markup=menu_service.membership_keyboard(check),
        )

    async def ensure_membership(message: Message, bot: Bot, use_cache: bool = True) -> bool:
        user_id = message.from_user.id if message.from_user else 0
        check = await channel_service.check_user(bot, user_id, use_cache=use_cache)
        if check.ok:
            return True
        user_service.set_state(user_id, OnboardingState.NEED_CHANNELS)
        await show_membership_gate(message, check)
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
            await message.answer(f"{suggestion} صدات کنم؟", reply_markup=menu_service.name_confirm())
            return
        user_service.set_state(user_id, OnboardingState.ASK_NAME_INPUT)
        await message.answer("دوست داری چی صدات کنم؟ فقط یک اسم کوتاه بفرست.")

    async def finish_onboarding(message: Message, name: str) -> None:
        user_id = message.from_user.id if message.from_user else 0
        user_service.save_display_name(user_id, name)
        memory_service.upsert_identity_name(user_id, name, message.message_id)
        await message.answer(
            f"خوبه، از این به بعد {name} صدات می‌کنم.\n"
            "از منو می‌تونی گفت‌وگوی تازه، حافظه‌ها و تنظیمات را ببینی.",
            reply_markup=menu_service.main_menu(),
        )

    async def start_flow(message: Message, bot: Bot) -> None:
        if not message.from_user:
            await message.answer("نتوانستم اطلاعات کاربرت را بخوانم. دوباره /start را بزن.")
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot, use_cache=False):
            return
        await ask_for_name(message)

    async def handle_name_text(message: Message) -> bool:
        if not message.from_user:
            return False
        profile = user_service.get(message.from_user.id)
        if profile is None or profile.onboarding_state not in {
            OnboardingState.ASK_NAME_INPUT,
            OnboardingState.NAME_AMBIGUOUS_CONFIRM,
        }:
            return False

        result = name_service.validate(message.text or "", allow_ambiguous=True)
        if not result.ok or not result.normalized:
            await message.answer(
                f"این اسم را نمی‌تونم ذخیره کنم: {result.reason}\n"
                "یک اسم واقعی و کوتاه بفرست؛ لینک، username، تبلیغ یا ایموجی تنها نباشد."
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

        await finish_onboarding(message, result.normalized)
        return True

    async def show_profile(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        profile = user_service.get(user_id)
        remaining = quota_service.remaining_today(user_id)
        if profile is None:
            await message.answer("اول /start را بزن تا پروفایلت ساخته شود.")
            return
        await message.answer(
            "پروفایل تو\n"
            f"نام: {profile.display_name or 'ثبت نشده'}\n"
            f"پلن: {profile.plan}\n"
            f"سهمیه امروز: {remaining} واحد"
        )

    async def show_memories(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        memories = memory_service.list_active(user_id, limit=30)
        if not memories:
            await message.answer("هنوز چیزی برایت ذخیره نکرده‌ام.")
            return
        lines = ["حافظه‌های فعال:"]
        lines.extend(f"{item.id}. {item.kind.value}: {item.summary}" for item in memories)
        await message.answer("\n".join(lines))

    @dispatcher.message(CommandStart())
    async def start_handler(message: Message, bot: Bot) -> None:
        await start_flow(message, bot)

    @dispatcher.message(Command("new"))
    async def new_handler(message: Message, bot: Bot) -> None:
        if not message.from_user:
            return
        user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await message.answer("گفت‌وگوی تازه شروع شد. هرچی می‌خوای بفرست.")

    @dispatcher.message(Command("profile"))
    async def profile_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await show_profile(message)

    @dispatcher.message(Command("memories", "memory"))
    async def memories_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await show_memories(message)

    @dispatcher.message(Command("forget"))
    async def forget_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        user_id = message.from_user.id if message.from_user else 0
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
            "تنظیمات فعلاً ساده است:\n"
            "برای تغییر نام، دوباره /start را بزن.\n"
            "برای حذف حافظه‌ها از /memories و /forget استفاده کن.",
            reply_markup=menu_service.main_menu(),
        )

    @dispatcher.message(Command("help"))
    async def help_handler(message: Message, bot: Bot) -> None:
        if message.from_user:
            user_service.upsert_telegram_user(profile_from_user(message.from_user))
        if not await ensure_membership(message, bot):
            return
        await message.answer(
            "راهنما\n"
            "/new شروع گفت‌وگوی تازه\n"
            "/profile پروفایل\n"
            "/memories حافظه‌ها\n"
            "/settings تنظیمات\n"
            "در پیام‌های عادی فقط متن بفرست؛ منو همیشه نمایش داده نمی‌شود تا گفتگو شلوغ نشود.",
            reply_markup=menu_service.main_menu(),
        )

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
        chat_id = parts[0]
        title = parts[1]
        join_url = parts[2] if len(parts) >= 3 and parts[2] else None
        is_private = len(parts) >= 4 and parts[3].lower() in {"private", "خصوصی", "1", "true"}
        channel = channel_service.add_channel(user_id, chat_id, title, join_url, is_private)
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
        deleted = channel_service.remove_channel(user_id, int(parts[1]))
        await message.answer("حذف شد." if deleted else "کانال پیدا نشد.")

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
        moved = channel_service.move_channel(user_id, int(parts[1]), int(parts[2]))
        await message.answer("مرتب‌سازی ذخیره شد." if moved else "کانال پیدا نشد.")

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
        reason = parts[3] if len(parts) >= 4 else None
        channel_service.grant_admin_bypass(user_id, target_id, minutes, reason)
        await message.answer(f"bypass برای {target_id} به مدت {minutes} دقیقه ثبت شد.")

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
        await ask_for_name(callback.message)

    @dispatcher.callback_query(F.data == "onboarding:name_confirm")
    async def confirm_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        if not profile or not profile.suggested_name:
            await callback.answer("اسم پیشنهادی پیدا نشد.")
            user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
            await callback.message.answer("چی صدات کنم؟")
            return
        await callback.answer("ثبت شد.")
        await finish_onboarding(callback.message, profile.suggested_name)

    @dispatcher.callback_query(F.data == "onboarding:name_change")
    async def change_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user_service.set_state(callback.from_user.id, OnboardingState.ASK_NAME_INPUT)
        await callback.answer()
        await callback.message.answer("باشه، اسم دلخواهت را بفرست.")

    @dispatcher.callback_query(F.data == "onboarding:ambiguous_confirm")
    async def ambiguous_name_callback(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        profile = user_service.get(callback.from_user.id)
        if not profile or not profile.pending_name:
            await callback.answer("نامی برای تأیید پیدا نشد.")
            return
        await callback.answer("ثبت شد.")
        await finish_onboarding(callback.message, profile.pending_name)

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
        if action == "new":
            await callback.message.answer("گفت‌وگوی تازه شروع شد. پیام بعدی‌ات را بفرست.")
        elif action == "memories":
            await show_memories(callback.message)
        elif action == "profile":
            await show_profile(callback.message)
        elif action == "settings":
            await callback.message.answer("تنظیمات فعلاً از همین منو و دستورهای /profile و /memories مدیریت می‌شود.")
        elif action == "help":
            await callback.message.answer("پیام متنی بفرست؛ نرگس جواب کوتاه و طبیعی می‌دهد. برای حافظه‌ها /memories را بزن.")

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
        if profile is None or profile.onboarding_state != OnboardingState.READY:
            await ask_for_name(message)
            return

        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        try:
            result = await chat_service.answer(message.from_user.id, message.chat.id, message.message_id, message.text.strip())
        except UserFacingError as exc:
            await message.answer(str(exc))
            return

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
