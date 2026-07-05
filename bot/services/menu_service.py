from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from bot.config import Settings
from bot.models.channel import MembershipCheck


class MenuService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def setup_commands(self, bot: Bot) -> None:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="شروع و ورود"),
                BotCommand(command="new", description="گفت‌وگوی جدید"),
                BotCommand(command="profile", description="پروفایل من"),
                BotCommand(command="memories", description="حافظه‌ها"),
                BotCommand(command="settings", description="تنظیمات"),
                BotCommand(command="help", description="راهنما"),
            ],
            scope=BotCommandScopeDefault(),
        )
        for admin_id in self.settings.admin_ids:
            await bot.set_my_commands(
                [
                    BotCommand(command="admin_channels", description="مدیریت کانال‌ها"),
                    BotCommand(command="admin_add_channel", description="افزودن کانال اجباری"),
                    BotCommand(command="admin_remove_channel", description="حذف کانال اجباری"),
                    BotCommand(command="admin_move_channel", description="مرتب‌سازی کانال"),
                    BotCommand(command="admin_bypass", description="bypass موقت عضویت"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )

    def main_menu(self) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(text="گفت‌وگوی جدید", callback_data="menu:new"),
                InlineKeyboardButton(text="حافظه‌ها", callback_data="menu:memories"),
            ],
            [
                InlineKeyboardButton(text="پروفایل", callback_data="menu:profile"),
                InlineKeyboardButton(text="تنظیمات", callback_data="menu:settings"),
            ],
            [
                InlineKeyboardButton(text="راهنما", callback_data="menu:help"),
                InlineKeyboardButton(text="پشتیبانی", url=self.settings.support_url or "https://t.me/"),
            ],
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

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
