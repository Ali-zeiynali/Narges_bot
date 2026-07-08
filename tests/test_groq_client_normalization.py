import unittest

from bot.services.groq_client import GroqChatClient


class GroqClientNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = object.__new__(GroqChatClient)

    def test_narges_state_payload_trims_long_reason(self) -> None:
        payload = self.client._normalize_narges_state_payload(
            {
                "mood": "calm",
                "energy": 70,
                "activity": "reading",
                "location": "home",
                "is_alone": True,
                "companions": ["friend"],
                "mind_topics": ["work"],
                "note": "n" * 400,
                "confidence": 0.8,
                "reason": "r" * 400,
            }
        )

        self.assertEqual(payload["companions"], [])
        self.assertEqual(len(payload["note"]), 180)
        self.assertEqual(len(payload["reason"]), 240)

    def test_image_selection_accepts_nested_image_request_shape(self) -> None:
        image_id, caption = self.client._extract_image_selection_payload(
            {
                "messages": [{"text": "sent"}],
                "image_request": {"image_id": "selfie_4", "caption": "caption"},
            }
        )

        self.assertEqual(image_id, "selfie_4")
        self.assertEqual(caption, "caption")

    def test_partial_json_reply_recovers_message_text(self) -> None:
        payload = self.client._try_loads_json('{"messages":[{"text":"hello there"')

        self.assertEqual(payload["messages"][0]["text"], "hello there")

    def test_clean_text_reply_extracts_from_partial_json(self) -> None:
        text = self.client._clean_text_reply('{"messages":[{"text":"main answer"')

        self.assertEqual(text, "main answer")

    def test_thinking_block_is_removed_before_json_parsing(self) -> None:
        payload = self.client._loads_json('<think>private chain</think>{"messages":[{"text":"visible"}]}')

        self.assertEqual(payload["messages"][0]["text"], "visible")

    def test_unclosed_thinking_block_recovers_json_payload(self) -> None:
        payload = self.client._loads_json('<think>private chain\n{"messages":[{"text":"visible"}]}')

        self.assertEqual(payload["messages"][0]["text"], "visible")

    def test_clean_text_reply_strips_thinking_and_code_blocks(self) -> None:
        text = self.client._clean_text_reply("<think>hidden</think>```python\nprint('x')\n```\nvisible")

        self.assertEqual(text, "visible")


if __name__ == "__main__":
    unittest.main()
