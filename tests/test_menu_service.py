import unittest

from bot.config import Settings
from bot.services.menu_service import MenuService


def make_settings() -> Settings:
    return Settings(
        telegram_token="t",
        telegram_proxy=None,
        groq_proxy=None,
        groq_api_key="g",
        groq_model="m",
        groq_temperature=0.7,
        groq_max_completion_tokens=512,
        max_request_tokens=3000,
        max_message_chars=4000,
        persona_version="v",
        database_path=":memory:",
        log_file="logs/test.log",
        log_level="INFO",
        admin_ids=(),
        support_url=None,
        free_daily_quota=40,
        free_monthly_quota=300,
        rate_limit_short_count=6,
        rate_limit_short_window_seconds=120,
        rate_limit_long_count=15,
        rate_limit_long_window_seconds=600,
        membership_cache_seconds=60,
        admin_bypass_minutes=60,
        debug_mode=False,
        debug_user_ids=(),
        name_transliteration_map={},
    )


class MenuServiceTests(unittest.TestCase):
    def test_capacity_keyboard_can_hide_phone_option(self) -> None:
        keyboard = MenuService(make_settings()).capacity_keyboard(phone_available=False)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertEqual(callbacks[:3], ["capacity:referral", "billing:stars_menu", "billing:card_menu"])
        self.assertIn("billing:stars_menu", callbacks)
        self.assertIn("billing:card_menu", callbacks)
        self.assertNotIn("capacity:phone", callbacks)

    def test_card_plans_keyboard_contains_toman_plans(self) -> None:
        keyboard = MenuService(make_settings()).card_plans_keyboard()
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

        self.assertIn("billing:card_plan:card_100", callbacks)
        self.assertIn("billing:card_plan:card_1000_discount", callbacks)


if __name__ == "__main__":
    unittest.main()
