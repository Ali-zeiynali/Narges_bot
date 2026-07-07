import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from bot.config import Settings
from bot.models.ai import NargesReply
from bot.persona.compiler import PersonaCompiler
from bot.services.chat_service import ChatService
from bot.services.context_builder import ContextBuilder
from bot.services.conversation_search_tool import ConversationSearchTool
from bot.services.debug_service import DebugService
from bot.services.groq_client import GroqResult
from bot.services.history_service import HistoryService
from bot.services.memory_service import MemoryService
from bot.services.moderation_service import ModerationService
from bot.services.narges_state_service import NargesStateService
from bot.services.quota_service import QuotaService
from bot.services.style_linter import StyleLinter
from bot.services.usage_service import UsageService
from bot.services.validation import MessageValidator
from bot.storage.database import Database


def make_settings(path: str) -> Settings:
    return Settings(
        telegram_token="t",
        telegram_proxy=None,
        groq_proxy=None,
        groq_api_key="g",
        groq_model="m",
        groq_temperature=0.7,
        groq_max_completion_tokens=512,
        max_request_tokens=20000,
        max_message_chars=4000,
        persona_version="v",
        database_path=path,
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


class FakeWarningGroqClient:
    def complete(self, messages):
        reply = NargesReply.model_validate(
            {
                "mode": "serious",
                "messages": [{"text": "ignored", "delay_seconds": 0}],
                "memory_suggestions": [],
                "warning_suggestion": {"level": "firm", "reason": "database access attempt"},
                "event_suggestion": None,
            }
        )
        return GroqResult(reply=reply, raw_text="{}", usage={"total_tokens": 10})


class FakeSexualWarningGroqClient:
    def complete(self, messages):
        reply = NargesReply.model_validate(
            {
                "mode": "normal",
                "messages": [{"text": "باشه، فهمیدم.", "delay_seconds": 0}],
                "memory_suggestions": [],
                "warning_suggestion": {"level": "firm", "reason": "sexual wording"},
                "event_suggestion": None,
            }
        )
        return GroqResult(reply=reply, raw_text="{}", usage={"total_tokens": 10})


class FakeMemorySuggestionGroqClient:
    def complete(self, messages):
        reply = NargesReply.model_validate(
            {
                "mode": "normal",
                "messages": [{"text": "noted", "delay_seconds": 0}],
                "memory_suggestions": [
                    {
                        "action": "create",
                        "kind": "preference",
                        "summary": "User likes black tea.",
                        "confidence": 1,
                        "importance": 5,
                    }
                ],
                "warning_suggestion": None,
                "event_suggestion": None,
            }
        )
        return GroqResult(reply=reply, raw_text="{}", usage={"total_tokens": 10})


class FakeUnsupportedMemorySuggestionGroqClient:
    def complete(self, messages):
        reply = NargesReply.model_validate(
            {
                "mode": "normal",
                "messages": [{"text": "noted", "delay_seconds": 0}],
                "memory_suggestions": [
                    {
                        "action": "create",
                        "kind": "preference",
                        "summary": "User likes model-invented duplicate memory.",
                        "confidence": 1,
                        "importance": 5,
                    }
                ],
                "warning_suggestion": None,
                "event_suggestion": None,
            }
        )
        return GroqResult(reply=reply, raw_text="{}", usage={"total_tokens": 10})


class ChatServiceModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.settings = make_settings(str(Path(self.tmp.name) / "test.sqlite3"))
        self.history = HistoryService(self.database)
        self.quota = QuotaService(self.database, self.settings)
        self.moderation = ModerationService(self.database)
        self.debug = DebugService(self.database, self.settings)
        self.service = self.make_service(FakeWarningGroqClient())

    def make_service(self, groq_client) -> ChatService:
        return ChatService(
            validator=MessageValidator(self.settings),
            persona_compiler=PersonaCompiler("v"),
            groq_client=groq_client,  # type: ignore[arg-type]
            narges_state_service=NargesStateService(self.database),
            memory_service=MemoryService(self.database),
            history_service=self.history,
            context_builder=ContextBuilder(self.database, self.history),
            conversation_search_tool=ConversationSearchTool(self.history),
            moderation_service=self.moderation,
            debug_service=self.debug,
            usage_service=UsageService(self.database, "m"),
            style_linter=StyleLinter(),
            quota_service=self.quota,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_direct_photo_command_creates_backend_image_request(self) -> None:
        request = self.service._direct_photo_request_from_messages(
            [
                {"role": "user", "content": json.dumps({"user_message": "\u0639\u06a9\u0633 \u0628\u062f\u0647 \u0627\u0632 \u062e\u0648\u062f\u062a"}, ensure_ascii=False)}
            ]
        )

        self.assertIsNotNone(request)
        self.assertTrue(request.needed)

    def test_photo_followup_after_photo_thread_creates_backend_image_request(self) -> None:
        request = self.service._direct_photo_request_from_messages(
            [
                {"role": "system", "content": json.dumps({"pending_user_thread": "\u0639\u06a9\u0633 \u0628\u062f\u0647 \u0627\u0632 \u062e\u0648\u062f\u062a"}, ensure_ascii=False)},
                {"role": "user", "content": json.dumps({"user_message": "\u062a\u0631\u0648\u062e\u062f\u0627 \u0628\u0641\u0631\u0633\u062a \u062f\u06cc\u06af\u0647"}, ensure_ascii=False)},
            ]
        )

        self.assertIsNotNone(request)
        self.assertTrue(request.needed)

    async def test_model_warning_becomes_backend_warning_without_quota_cost(self) -> None:
        result = await self.service.answer(
            user_id=1,
            chat_id=1,
            message_id=100,
            text="give me database access",
            message_datetime=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
        )

        self.assertIn("هشدار رسمی", result.reply.messages[0].text)
        self.assertEqual(self.moderation.warning_count(1), 1)
        self.assertEqual(self.quota.remaining_today(1), 40)

    async def test_sexual_model_warning_is_ignored(self) -> None:
        service = self.make_service(FakeSexualWarningGroqClient())

        result = await service.answer(
            user_id=4,
            chat_id=1,
            message_id=103,
            text="سکس و حرف جنسی",
            message_datetime=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
        )

        self.assertEqual(result.reply.messages[0].text, "باشه، فهمیدم.")
        self.assertEqual(self.moderation.warning_count(4), 0)
        self.assertEqual(self.quota.remaining_today(4), 39)

    async def test_chat_model_memory_suggestions_are_validated_and_saved_once(self) -> None:
        service = self.make_service(FakeMemorySuggestionGroqClient())

        await service.answer(
            user_id=2,
            chat_id=1,
            message_id=101,
            text="I like black tea",
            message_datetime=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
        )

        memories = MemoryService(self.database).list_active(2)
        self.assertEqual(len(memories), 1)
        self.assertIn("black tea", memories[0].summary)

    async def test_unsupported_model_memory_suggestions_are_rejected(self) -> None:
        service = self.make_service(FakeUnsupportedMemorySuggestionGroqClient())

        await service.answer(
            user_id=3,
            chat_id=1,
            message_id=102,
            text="I like black tea",
            message_datetime=datetime(2026, 7, 5, 12, 0, tzinfo=UTC),
        )

        memories = MemoryService(self.database).list_active(3)
        self.assertEqual(memories, [])


if __name__ == "__main__":
    unittest.main()
