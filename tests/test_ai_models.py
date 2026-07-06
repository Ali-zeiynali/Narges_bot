import unittest

from pydantic import ValidationError

from bot.models.ai import NargesReply


class NargesReplyModelTests(unittest.TestCase):
    def test_accepts_valid_structured_reply(self) -> None:
        reply = NargesReply.model_validate(
            {
                "mode": "normal",
                "messages": [{"text": "Okay, I will do it.", "delay_seconds": 0.2}],
                "memory_suggestions": [],
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
                        {"text": "Okay.", "delay_seconds": 0.1},
                        {"text": "Okay.", "delay_seconds": 0.2},
                    ],
                }
            )

    def test_normalizes_provider_payload_before_validation(self) -> None:
        reply = NargesReply.validate_provider_payload(
            {
                "tone": "supportive",
                "messages": [{"text": "This is a healthy answer.", "delay_seconds": 0}],
                "ignored_delta": {"respect": -10},
                "warning_suggestion": {"level": "soft", "text": "soft boundary", "extra": "ignored"},
            }
        )

        self.assertEqual(reply.mode, "supportive")
        self.assertFalse(hasattr(reply, "ignored_delta"))
        self.assertEqual(reply.warning_suggestion.reason, "soft boundary")


if __name__ == "__main__":
    unittest.main()
