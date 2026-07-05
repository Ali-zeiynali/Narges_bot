import tempfile
import unittest
from pathlib import Path

from bot.services.moderation_service import ModerationService
from bot.storage.database import Database


class ModerationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = ModerationService(self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_three_warnings_block_for_seven_days(self) -> None:
        self.service.apply_model_warning(1, "malicious boundary attempt", 10)
        self.service.apply_model_warning(1, "malicious boundary attempt", 11)
        result = self.service.apply_model_warning(1, "malicious boundary attempt", 12)
        status = self.service.get_block_status(1)

        self.assertEqual(result.warning_count, 3)
        self.assertTrue(status.blocked)
        self.assertIn("🔴", result.message)

    def test_fifth_warning_blocks_for_about_one_month(self) -> None:
        for index in range(5):
            result = self.service.apply_model_warning(1, "database access attempt", index)

        status = self.service.get_block_status(1)

        self.assertEqual(result.warning_count, 5)
        self.assertTrue(status.blocked)
        self.assertIsNotNone(status.blocked_until)


if __name__ == "__main__":
    unittest.main()
