import tempfile
import unittest
from pathlib import Path

from bot.models.user import OnboardingState, TelegramUserProfile
from bot.services.user_service import UserService
from bot.storage.database import Database


class UserServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = UserService(self.database)
        self.service.upsert_telegram_user(TelegramUserProfile(1, "ali", "Ali", None, "fa"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_name_then_gender_completes_onboarding(self) -> None:
        self.service.save_display_name(1, "علی")
        profile = self.service.get(1)

        self.assertEqual(profile.onboarding_state, OnboardingState.ASK_GENDER)

        self.service.save_gender(1, "male")
        profile = self.service.get(1)

        self.assertEqual(profile.gender, "male")
        self.assertEqual(profile.onboarding_state, OnboardingState.READY)


if __name__ == "__main__":
    unittest.main()
