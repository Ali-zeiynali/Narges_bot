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
        initial = self.service.get(1)
        self.assertEqual(initial.onboarding_state, OnboardingState.NOT_STARTED)

        self.service.save_display_name(1, "علی")
        profile = self.service.get(1)

        self.assertEqual(profile.onboarding_state, OnboardingState.ASK_GENDER)

        self.service.save_gender(1, "male")
        profile = self.service.get(1)

        self.assertEqual(profile.gender, "male")
        self.assertEqual(profile.onboarding_state, OnboardingState.READY)

    def test_profile_completion_tracks_bio_retries_and_one_time_quota_offer(self) -> None:
        self.service.start_profile_completion(1)
        self.assertEqual(self.service.get(1).profile_completion_state, "awaiting_bio")

        self.assertFalse(self.service.register_profile_invalid_attempt(1))
        self.assertTrue(self.service.register_profile_invalid_attempt(1))
        self.assertEqual(self.service.get(1).profile_completion_state, "idle")

        self.service.start_profile_completion(1)
        self.service.save_biography(1, "من موسیقی و برنامه نویسی را دوست دارم")
        profile = self.service.get(1)
        self.assertEqual(profile.profile_completion_state, "awaiting_phone")
        self.assertIn("موسیقی", profile.biography)

        self.assertTrue(self.service.should_offer_profile_for_quota(1))
        self.assertFalse(self.service.should_offer_profile_for_quota(1))


if __name__ == "__main__":
    unittest.main()
