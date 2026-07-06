from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.config import Settings
from bot.models.channel import MembershipCheck
from bot.services.billing_service import STAR_PLANS


class MenuService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def setup_commands(self, bot: Bot) -> None:
        await bot.set_my_commands(
            [BotCommand(command="start", description="شروع")],
            scope=BotCommandScopeDefault(),
        )

        admin_commands = [
            BotCommand(command="admin_channels", description="مدیریت کانال‌ها"),
            BotCommand(command="admin_add_channel", description="افزودن کانال اجباری"),
            BotCommand(command="admin_remove_channel", description="حذف کانال اجباری"),
            BotCommand(command="admin_move_channel", description="مرتب‌سازی کانال"),
            BotCommand(command="admin_bypass", description="bypass موقت عضویت"),
        ]
        debug_commands = []
        if self.settings.debug_mode:
            debug_commands = [
                BotCommand(command="debug_account", description="دیباگ حساب"),
                BotCommand(command="debug_memories", description="دیباگ حافظه‌ها"),
                BotCommand(command="debug_state", description="دیباگ وضعیت نرگس"),
                BotCommand(command="debug_quota", description="دیباگ سهمیه"),
                BotCommand(command="debug_logs", description="دیباگ لاگ‌ها"),
                BotCommand(command="debug_all", description="دیباگ کامل"),
            ]

        for admin_id in set(self.settings.admin_ids) | set(self.settings.debug_user_ids):
            await bot.set_my_commands(admin_commands + debug_commands, scope=BotCommandScopeChat(chat_id=admin_id))

    def reply_menu(self, debug: bool = False) -> ReplyKeyboardMarkup:
        keyboard = [
            [KeyboardButton(text="👤 پروفایل"), KeyboardButton(text="⚡ افزایش ظرفیت")],
            [KeyboardButton(text="💬 راهنما")],
        ]
        if debug:
            keyboard[1].append(KeyboardButton(text="🧠 حافظه‌ها"))
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, selective=True)

    def main_menu(self, debug: bool = False) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(text="👤 پروفایل", callback_data="menu:profile"),
                InlineKeyboardButton(text="⚡ افزایش ظرفیت", callback_data="capacity:open"),
            ],
            [
                InlineKeyboardButton(text="💬 راهنما", callback_data="menu:help"),
                InlineKeyboardButton(text="🛟 پشتیبانی", url=self.settings.support_url or "https://t.me/"),
            ],
        ]
        if debug:
            rows.insert(1, [InlineKeyboardButton(text="🧠 حافظه‌ها", callback_data="menu:memories")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def account_keyboard(self, debug: bool = False) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(text="✏️ ویرایش اسم", callback_data="account:edit_name"),
                InlineKeyboardButton(text="⚧️ ویرایش جنسیت", callback_data="account:edit_gender"),
            ],
            [InlineKeyboardButton(text="⚡ افزایش ظرفیت", callback_data="capacity:open")],
        ]
        if debug:
            rows.append([InlineKeyboardButton(text="🧠 حافظه‌ها", callback_data="menu:memories")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def capacity_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="⭐ خرید ظرفیت", callback_data="billing:stars_menu"),
                    InlineKeyboardButton(text="📱 هدیه شماره", callback_data="capacity:phone"),
                ],
            ]
        )

    def stars_plans_keyboard(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text=f"⭐ {plan.message_quota} پیام | {plan.stars_cost} Stars", callback_data=f"billing:plan:{plan.id}")]
            for plan in STAR_PLANS
        ]
        rows.append([InlineKeyboardButton(text="↩️ برگشت", callback_data="billing:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def phone_request_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 ارسال شماره همین اکانت تلگرام", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
            selective=True,
        )

    def name_confirm(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ آره، همین", callback_data="onboarding:name_confirm"),
                    InlineKeyboardButton(text="✏️ یه اسم دیگه", callback_data="onboarding:name_change"),
                ]
            ]
        )

    def ambiguous_name_confirm(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ تأییدش می‌کنم", callback_data="onboarding:ambiguous_confirm"),
                    InlineKeyboardButton(text="✏️ اصلاح اسم", callback_data="onboarding:name_change"),
                ]
            ]
        )

    def gender_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🙋‍♀️ دخترم", callback_data="onboarding:gender:female"),
                    InlineKeyboardButton(text="🙋‍♂️ پسرم", callback_data="onboarding:gender:male"),
                ],
                [InlineKeyboardButton(text="✨ ترجیح می‌دم نگم", callback_data="onboarding:gender:unspecified")],
            ]
        )

    def membership_keyboard(self, check: MembershipCheck) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for item in check.missing:
            url = item.channel.join_url or self._public_channel_url(item.channel.chat_id)
            rows.append([InlineKeyboardButton(text=f"📣 عضویت در {item.channel.title}", url=url)])
        rows.append([InlineKeyboardButton(text="✅ عضو شدم، بررسی کن", callback_data="onboarding:check_channels")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _public_channel_url(self, chat_id: str) -> str:
        if chat_id.startswith("@"):
            return f"https://t.me/{chat_id[1:]}"
        return "https://t.me/"
