import tempfile
import unittest
from pathlib import Path

from bot.services.required_channel_service import RequiredChannelService
from bot.storage.database import Database


class RequiredChannelServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = RequiredChannelService(self.database, cache_seconds=60, admin_ids=())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_empty_required_channels_allow_user(self) -> None:
        check = await self.service.check_user(bot=None, user_id=1)  # type: ignore[arg-type]

        self.assertTrue(check.ok)
        self.assertEqual(check.missing, [])

    def test_admin_can_add_remove_and_audit_channel(self) -> None:
        channel = self.service.add_channel(
            admin_id=10,
            chat_id="@test_channel",
            title="Test",
            join_url="https://t.me/test_channel",
            is_private=False,
        )
        self.assertEqual(len(self.service.list_active()), 1)

        removed = self.service.remove_channel(10, channel.id)

        self.assertTrue(removed)
        self.assertEqual(len(self.service.list_active()), 0)
        self.assertEqual(len(self.service.list_all()), 0)


if __name__ == "__main__":
    unittest.main()
