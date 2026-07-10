import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from bot.admin.services import AdminDataService
from bot.config import Settings
from bot.services.history_service import HistoryService
from bot.storage.database import Database
from bot.storage.orm import ConversationMessageORM, GroupChatORM, MediaFileORM, UserORM
from bot.services.billing_service import BillingService


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


class AdminBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Database(str(Path(self.tmp.name) / "source.sqlite3"))
        self.target = Database(str(Path(self.tmp.name) / "target.sqlite3"))
        self.source.migrate()
        self.target.migrate()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_backup_import_appends_without_overwriting_existing_primary_keys(self) -> None:
        with self.source.orm.session() as session:
            session.add(UserORM(telegram_id=1, username="source", first_name="Source"))
        HistoryService(self.source).add(1, "user", "hello from source")

        with self.target.orm.session() as session:
            session.add(UserORM(telegram_id=1, username="target", first_name="Target"))
        HistoryService(self.target).add(1, "user", "existing target message")

        source_service = AdminDataService(self.source, make_settings())
        target_service = AdminDataService(self.target, make_settings())
        payload = source_service.export_backup()
        report = target_service.import_backup(payload)

        with self.target.orm.session() as session:
            user = session.get(UserORM, 1)
            messages = session.scalars(select(ConversationMessageORM)).all()

        self.assertEqual(user.username, "target")
        self.assertEqual(len(messages), 2)
        self.assertGreaterEqual(report["inserted"], 2)
        self.assertGreaterEqual(report["skipped"], 1)

    def test_backup_round_trips_media_binary_and_dashboard_messages(self) -> None:
        with self.source.orm.session() as session:
            session.add(UserORM(telegram_id=11, username="photo", first_name="Photo"))
            session.add(
                MediaFileORM(
                    user_id=11,
                    telegram_file_id="file-id",
                    media_kind="image",
                    mime_type="image/jpeg",
                    storage_path="",
                    file_bytes=b"\x00\x01photo-bytes\xff",
                    file_size=14,
                )
            )
        HistoryService(self.source).add(11, "user", "dashboard message")

        source_service = AdminDataService(self.source, make_settings())
        target_service = AdminDataService(self.target, make_settings())
        payload = source_service.export_backup()
        json.dumps(payload, ensure_ascii=False)
        target_service.import_backup(payload)

        with self.target.orm.session() as session:
            media = session.scalar(select(MediaFileORM).where(MediaFileORM.user_id == 11))
        self.assertEqual(media.file_bytes, b"\x00\x01photo-bytes\xff")
        snapshot = source_service.dashboard()
        self.assertEqual(snapshot.recent_messages[0]["text"], "dashboard message")

    def test_admin_invoice_receipt_is_visible_and_approval_is_idempotent(self) -> None:
        with self.source.orm.session() as session:
            session.add(UserORM(telegram_id=21, username="payer", first_name="Payer"))
            media = MediaFileORM(
                user_id=21,
                telegram_file_id="receipt-file",
                media_kind="image",
                mime_type="image/jpeg",
                storage_path="",
                file_bytes=b"receipt",
                file_size=7,
            )
            session.add(media)
            session.flush()
            media_id = media.id
        billing = BillingService(self.source)
        invoice = billing.create_card_invoice(21, "card_100")
        billing.attach_card_receipt(21, f"photo:{media_id}:receipt-file")
        service = AdminDataService(self.source, make_settings())

        invoice_view = service.invoices()
        self.assertEqual(invoice_view["receipt_media_by_invoice"][invoice.invoice_id]["id"], media_id)
        first = service.review_invoice(invoice.invoice_id, approve=True)
        second = service.review_invoice(invoice.invoice_id, approve=True)

        self.assertTrue(first[0])
        self.assertTrue(second[0])
        self.assertEqual(service.quota_service.account_quota(21).extra_remaining, 100 * 5)

    def test_admin_cannot_approve_card_invoice_without_receipt(self) -> None:
        billing = BillingService(self.source)
        invoice = billing.create_card_invoice(22, "card_100")
        service = AdminDataService(self.source, make_settings())

        accepted, current, reason = service.review_invoice(invoice.invoice_id, approve=True)

        self.assertFalse(accepted)
        self.assertEqual(current.status.value, "pending")
        self.assertIn("receipt", reason)

    def test_users_sort_handles_mixed_naive_and_aware_datetimes(self) -> None:
        database = Database(str(Path(self.tmp.name) / "mixed.sqlite3"))
        database.migrate()
        with database.orm.session() as session:
            session.add(
                UserORM(
                    telegram_id=10,
                    username="aware",
                    first_name="Aware",
                    created_at=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
                    updated_at=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
                )
            )
            session.add(
                UserORM(
                    telegram_id=20,
                    username="naive",
                    first_name="Naive",
                    created_at=datetime(2026, 7, 6, 12, 0),
                    updated_at=datetime(2026, 7, 6, 12, 0),
                )
            )
        service = AdminDataService(database, make_settings())

        users = service.users(sort="created")

        self.assertEqual([item["telegram_id"] for item in users], [20, 10])

    def test_gender_sort_places_missing_gender_last(self) -> None:
        database = Database(str(Path(self.tmp.name) / "gender.sqlite3"))
        database.migrate()
        with database.orm.session() as session:
            session.add(UserORM(telegram_id=10, username="none", first_name="None", gender=None))
            session.add(UserORM(telegram_id=20, username="male", first_name="Male", gender="male"))
            session.add(UserORM(telegram_id=30, username="female", first_name="Female", gender="female"))
        service = AdminDataService(database, make_settings())

        users = service.users(sort="gender")

        self.assertEqual([item["telegram_id"] for item in users], [30, 20, 10])

    def test_active_only_user_filter_keeps_users_with_messages(self) -> None:
        database = Database(str(Path(self.tmp.name) / "active-users.sqlite3"))
        database.migrate()
        with database.orm.session() as session:
            session.add(UserORM(telegram_id=10, username="inactive", first_name="Inactive"))
            session.add(UserORM(telegram_id=20, username="active", first_name="Active"))
        HistoryService(database).add(20, "user", "hello")
        service = AdminDataService(database, make_settings())

        users = service.users(active_only=True)

        self.assertEqual([item["telegram_id"] for item in users], [20])

    def test_hidden_group_statuses_are_removed_from_admin_group_views(self) -> None:
        database = Database(str(Path(self.tmp.name) / "groups.sqlite3"))
        database.migrate()
        now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
        with database.orm.session() as session:
            session.add(GroupChatORM(chat_id=1, title="Active", chat_type="supergroup", bot_status="member", active=True, last_seen_at=now))
            session.add(GroupChatORM(chat_id=2, title="Left", chat_type="supergroup", bot_status="left", active=False, last_seen_at=now))
        service = AdminDataService(database, make_settings())

        groups = service.group_chats()
        group_messages = service.group_messages()

        self.assertEqual([group.chat_id for group in groups], [1])
        self.assertEqual([group.chat_id for group in group_messages["groups"]], [1])
        self.assertEqual(service.target_group_ids(), [1])

    def test_all_messages_include_private_and_group_rows(self) -> None:
        database = Database(str(Path(self.tmp.name) / "user-group-messages.sqlite3"))
        database.migrate()
        now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
        with database.orm.session() as session:
            session.add(UserORM(telegram_id=1, username="u", first_name="U"))
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=1,
                    role="user",
                    message_type="chat",
                    text="private",
                    text_hash="p",
                    created_at=now,
                )
            )
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=-100,
                    telegram_message_id=2,
                    role="user",
                    message_type="group_mention",
                    text="group",
                    text_hash="g",
                    created_at=now,
                )
            )
        service = AdminDataService(database, make_settings())

        filtered = service.messages(user_id=1)["messages"]
        global_messages = service.messages()["messages"]
        detail = service.user_detail(1)["messages"]

        self.assertEqual({row.text for row in filtered}, {"private", "group"})
        self.assertEqual([row.text for row in global_messages], ["group", "private"])
        self.assertEqual({row.text for row in detail}, {"private", "group"})

    def test_group_messages_hide_unhandled_observed_rows(self) -> None:
        database = Database(str(Path(self.tmp.name) / "group-panel.sqlite3"))
        database.migrate()
        now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
        with database.orm.session() as session:
            session.add(GroupChatORM(chat_id=-100, title="Group", chat_type="supergroup", bot_status="member", active=True, last_seen_at=now))
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=-100,
                    telegram_message_id=1,
                    role="user",
                    message_type="group_observed",
                    text="ordinary",
                    text_hash="o",
                    created_at=now,
                )
            )
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=-100,
                    telegram_message_id=2,
                    role="user",
                    message_type="group_mention",
                    text="mention",
                    text_hash="m",
                    ai_request_payload_json=json.dumps({"reply_to_message_id": 1}),
                    created_at=now,
                )
            )
        service = AdminDataService(database, make_settings())

        messages = service.group_messages()

        self.assertEqual([row.text for row in messages["messages"]], ["mention"])
        self.assertEqual([row["text"] for row in messages["timeline"]], ["mention"])
        self.assertEqual(messages["timeline"][0]["reply_preview"]["text"], "ordinary")
        self.assertEqual(messages["counts"]["all"], 2)
        self.assertEqual(messages["counts"]["observed"], 1)

        mention = messages["messages"][0]
        detail = service.message_detail(mention.id)
        self.assertEqual(detail["group"].title, "Group")
        self.assertEqual(detail["group"].chat_id, -100)

    def test_media_gallery_includes_following_assistant_reply(self) -> None:
        database = Database(str(Path(self.tmp.name) / "media.sqlite3"))
        database.migrate()
        now = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
        with database.orm.session() as session:
            session.add(UserORM(telegram_id=1, username="u", first_name="U"))
            session.add(
                MediaFileORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=10,
                    telegram_file_id="photo",
                    media_kind="image",
                    mime_type="image/jpeg",
                    storage_path="",
                    file_size=4,
                    created_at=now,
                )
            )
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=1,
                    telegram_message_id=10,
                    role="user",
                    message_type="chat",
                    text="[image]",
                    text_hash="u",
                    created_at=now,
                )
            )
            session.add(
                ConversationMessageORM(
                    user_id=1,
                    chat_id=1,
                    role="assistant",
                    message_type="chat",
                    text="model answer",
                    text_hash="a",
                    created_at=now,
                )
            )
        service = AdminDataService(database, make_settings())

        media = service.media()["media"]

        self.assertEqual(media[0]["assistant_reply"]["text"], "model answer")


if __name__ == "__main__":
    unittest.main()
