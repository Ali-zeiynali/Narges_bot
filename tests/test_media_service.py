import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.config import Settings
from bot.services.media_service import MediaStorageService
from bot.storage.database import Database
from bot.storage.orm import MediaFileORM


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_token="t",
        telegram_proxy=None,
        groq_proxy=None,
        groq_api_key="g",
        groq_model="m",
        groq_temperature=0.7,
        groq_max_completion_tokens=512,
        max_request_tokens=4096,
        max_message_chars=4000,
        persona_version="v",
        database_path=":memory:",
        log_file="logs/test.log",
        log_level="INFO",
        admin_ids=(),
        support_url=None,
        free_daily_quota=3,
        free_monthly_quota=300,
        rate_limit_short_count=2,
        rate_limit_short_window_seconds=120,
        rate_limit_long_count=10,
        rate_limit_long_window_seconds=600,
        membership_cache_seconds=60,
        admin_bypass_minutes=60,
        debug_mode=False,
        debug_user_ids=(),
        name_transliteration_map={},
        media_storage_dir=str(tmp_path / "media"),
    )


class MediaStorageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.database = Database(str(self.tmp_path / "test.sqlite3"))
        self.database.migrate()
        self.service = MediaStorageService(make_settings(self.tmp_path), self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_image_count_today_ignores_old_and_non_image_media(self) -> None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            session.add(
                MediaFileORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=1,
                    telegram_file_id="photo-new",
                    media_kind="image",
                    mime_type="image/jpeg",
                    storage_path=str(self.tmp_path / "new.jpg"),
                    file_size=10,
                    created_at=now,
                )
            )
            session.add(
                MediaFileORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=2,
                    telegram_file_id="voice-new",
                    media_kind="voice",
                    mime_type="audio/ogg",
                    storage_path=str(self.tmp_path / "voice.ogg"),
                    file_size=10,
                    created_at=now,
                )
            )
            session.add(
                MediaFileORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=3,
                    telegram_file_id="photo-old",
                    media_kind="image",
                    mime_type="image/jpeg",
                    storage_path=str(self.tmp_path / "old.jpg"),
                    file_size=10,
                    created_at=now - timedelta(days=1),
                )
            )

        self.assertEqual(self.service.image_count_today(1), 1)


if __name__ == "__main__":
    unittest.main()
