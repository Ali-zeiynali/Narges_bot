import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from bot.models.ai import NargesReply
from bot.services.group_ai_service import GroupAIResult, GroupAIService
from bot.services.history_service import HistoryService
from bot.storage.database import Database
from bot.storage.orm import ConversationMessageORM


class FakeUsageService:
    def log(self, *args, **kwargs) -> None:
        return None


class GroupAIServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "group-ai.sqlite3"))
        self.database.migrate()
        self.service = GroupAIService(
            ai_provider_client=object(),
            narges_state_service=object(),
            memory_service=object(),
            history_service=HistoryService(self.database),
            debug_service=object(),
            usage_service=FakeUsageService(),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_store_turn_returns_assistant_row_id(self) -> None:
        result = GroupAIResult(
            reply=NargesReply.from_text("reply"),
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            provider="test-provider",
            model="test-model",
            estimated_tokens=3,
        )

        assistant_id = self.service._store_turn(
            user_id=10,
            chat_id=-100,
            message_id=50,
            user_text="hello",
            assistant_text="reply",
            message_datetime=datetime.now(UTC),
            result=result,
            request_payload={"task": "test"},
            message_type="group_mention",
        )

        with self.database.orm.session() as session:
            row = session.get(ConversationMessageORM, assistant_id)
            rows = session.scalars(select(ConversationMessageORM).order_by(ConversationMessageORM.id)).all()

        self.assertEqual(row.role, "assistant")
        self.assertEqual(row.text, "reply")
        self.assertEqual([item.role for item in rows], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
