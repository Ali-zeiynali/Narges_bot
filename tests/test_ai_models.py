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

    def test_plain_text_reply_is_supported(self) -> None:
        reply = NargesReply.from_text("Plain Telegram answer")

        self.assertEqual(reply.messages[0].text, "Plain Telegram answer")
        self.assertEqual(reply.memory_suggestions, [])

    def test_image_request_is_normalized(self) -> None:
        reply = NargesReply.validate_provider_payload(
            {
                "text": "اینم برای تو",
                "image_request": {
                    "needed": True,
                    "reason": "user asked for a selfie",
                    "prompt": "casual selfie",
                    "caption": "اینم عکس",
                },
            }
        )

        self.assertTrue(reply.image_request.needed)
        self.assertEqual(reply.image_request.prompt, "casual selfie")

    def test_invalid_memory_suggestion_shape_is_normalized(self) -> None:
        reply = NargesReply.validate_provider_payload(
            {
                "text": "یادم می‌مونه.",
                "memory_suggestions": [
                    {"id": 4, "action": "create", "summary": "کاربر موز دوست دارد"}
                ],
            }
        )

        self.assertEqual(reply.memory_suggestions[0].kind, "preference")
        self.assertEqual(reply.memory_suggestions[0].confidence, 0.75)

    def test_conversation_state_and_memory_id_are_normalized(self) -> None:
        reply = NargesReply.validate_provider_payload(
            {
                "text": "noted",
                "conversation_state": "sexual",
                "memory_suggestions": [
                    {
                        "id": 9,
                        "action": "replace",
                        "kind": "user_state",
                        "summary": "User feels calmer when replies are warm.",
                        "confidence": 0.8,
                    }
                ],
            }
        )

        self.assertEqual(reply.conversation_state, "sexual")
        self.assertEqual(reply.memory_suggestions[0].memory_id, 9)
        self.assertEqual(reply.memory_suggestions[0].kind, "user_state")


if __name__ == "__main__":
    unittest.main()
