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
            [
                BotCommand(command="start", description="شروع و ورود"),
                BotCommand(command="new", description="گفت‌وگوی جدید"),
                BotCommand(command="profile", description="پروفایل من"),
                BotCommand(command="account", description="حساب کاربری"),
                BotCommand(command="memories", description="حافظه‌ها"),
                BotCommand(command="settings", description="تنظیمات"),
                BotCommand(command="help", description="راهنما"),
            ],
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
                BotCommand(command="debug_account", description="دیباگ حساب کاربر"),
                BotCommand(command="debug_memories", description="دیباگ حافظه‌ها"),
                BotCommand(command="debug_state", description="دیباگ وضعیت نرگس"),
                BotCommand(command="debug_quota", description="دیباگ سهمیه"),
                BotCommand(command="debug_logs", description="دیباگ لاگ‌ها"),
                BotCommand(command="debug_all", description="دیباگ کامل داده‌ها"),
            ]

        for admin_id in set(self.settings.admin_ids) | set(self.settings.debug_user_ids):
            await bot.set_my_commands(
                admin_commands + debug_commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )

    def main_menu(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="گفت‌وگوی جدید", callback_data="menu:new"),
                    InlineKeyboardButton(text="حافظه‌ها", callback_data="menu:memories"),
                ],
                [
                    InlineKeyboardButton(text="پروفایل", callback_data="menu:profile"),
                    InlineKeyboardButton(text="تنظیمات", callback_data="menu:settings"),
                ],
                [
                    InlineKeyboardButton(text="حساب کاربری", callback_data="menu:account"),
                    InlineKeyboardButton(text="افزایش ظرفیت", callback_data="capacity:open"),
                ],
                [
                    InlineKeyboardButton(text="راهنما", callback_data="menu:help"),
                    InlineKeyboardButton(text="پشتیبانی", url=self.settings.support_url or "https://t.me/"),
                ],
            ]
        )

    def account_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="ویرایش نام کاربری", callback_data="account:edit_name"),
                    InlineKeyboardButton(text="افزایش ظرفیت", callback_data="capacity:open"),
                ],
                [InlineKeyboardButton(text="حافظه‌ها", callback_data="menu:memories")],
            ]
        )

    def capacity_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="خرید با Stars", callback_data="billing:stars_menu")],
                [InlineKeyboardButton(text="ارسال شماره موبایل (۳۰ پیام)", callback_data="capacity:phone")],
            ]
        )

    def stars_plans_keyboard(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(text=f"{plan.message_quota} پیام → {plan.stars_cost} Stars", callback_data=f"billing:plan:{plan.id}")]
            for plan in STAR_PLANS
        ]
        rows.append([InlineKeyboardButton(text="بازگشت", callback_data="billing:back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def phone_request_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="ارسال شماره موبایل اکانت تلگرام", request_contact=True)],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
            selective=True,
        )

    def name_confirm(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="بله، همین خوبه", callback_data="onboarding:name_confirm"),
                    InlineKeyboardButton(text="اسم دیگه می‌گم", callback_data="onboarding:name_change"),
                ]
            ]
        )

    def ambiguous_name_confirm(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="تأییدش می‌کنم", callback_data="onboarding:ambiguous_confirm"),
                    InlineKeyboardButton(text="اسم دیگری می‌نویسم", callback_data="onboarding:name_change"),
                ]
            ]
        )

    def membership_keyboard(self, check: MembershipCheck) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for item in check.missing:
            url = item.channel.join_url or self._public_channel_url(item.channel.chat_id)
            rows.append([InlineKeyboardButton(text=f"عضویت در {item.channel.title}", url=url)])
        rows.append([InlineKeyboardButton(text="عضو شدم، بررسی کن", callback_data="onboarding:check_channels")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _public_channel_url(self, chat_id: str) -> str:
        if chat_id.startswith("@"):
            return f"https://t.me/{chat_id[1:]}"
        return "https://t.me/"
