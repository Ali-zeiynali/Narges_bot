import unittest

from pydantic import ValidationError

from bot.models.ai import NargesReply


class NargesReplyModelTests(unittest.TestCase):
    def test_accepts_valid_structured_reply(self) -> None:
        reply = NargesReply.model_validate(
            {
                "mode": "normal",
                "messages": [{"text": "باشه، انجامش می‌دهم.", "delay_seconds": 0.2}],
                "memory_suggestions": [],
                "relationship_delta": {},
                "warning_suggestion": None,
                "event_suggestion": None,
            }
        )

        self.assertEqual(reply.mode, "normal")
        self.assertEqual(len(reply.messages), 1)

    def test_rejects_duplicate_messages(self) -> None:
        with self.assertRaises(ValidationError):
            NargesReply.model_validate(
                {
                    "mode": "short",
                    "messages": [
                        {"text": "باشه.", "delay_seconds": 0.1},
                        {"text": "باشه.", "delay_seconds": 0.2},
                    ],
                    "relationship_delta": {},
                }
            )


if __name__ == "__main__":
    unittest.main()
