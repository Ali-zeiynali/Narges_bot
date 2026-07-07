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


if __name__ == "__main__":
    unittest.main()
