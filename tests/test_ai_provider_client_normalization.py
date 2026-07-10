import unittest
import threading

from bot.services.ai_provider_client import AIProviderClient, ProviderConfig, ProviderRequestError


class AIProviderClientNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = object.__new__(AIProviderClient)

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

    def _routing_client(self) -> AIProviderClient:
        client = object.__new__(AIProviderClient)
        client.providers = [
            ProviderConfig(
                id="provider-0",
                name="provider",
                kind="openai_compatible",
                model="model",
                api_keys=("key-0", "key-1"),
                status_name="provider-0",
            )
        ]
        client._key_cursors = {}
        client._key_cursor_lock = threading.Lock()
        client._provider_disabled_until = {}
        client._reload_providers_if_changed = lambda: None
        client._key_is_temporarily_disabled = lambda *_args: False
        client._record_key_failure = lambda *_args: None
        client._record_key_success = lambda *_args: None
        return client

    def test_keys_are_used_round_robin(self) -> None:
        client = self._routing_client()
        seen = []

        def request(_provider, api_key, *_args, **_kwargs):
            seen.append(api_key)
            return "{}", {"total_tokens": 1}, "provider", "model", None

        client._request_provider = request
        args = ([{"role": "user", "content": "x"}], {}, "chat")
        client._complete_raw(*args, max_completion_tokens=10, temperature=0.1, forced_provider=None)
        client._complete_raw(*args, max_completion_tokens=10, temperature=0.1, forced_provider=None)

        self.assertEqual(seen, ["key-0", "key-1"])

    def test_two_matching_key_failures_cool_down_provider(self) -> None:
        client = self._routing_client()

        def request(*_args, **_kwargs):
            raise ProviderRequestError("provider", "HTTP 500", status_code=500, retry_after_seconds=120)

        client._request_provider = request
        with self.assertRaises(ProviderRequestError):
            client._complete_raw(
                [{"role": "user", "content": "x"}],
                {},
                "chat",
                max_completion_tokens=10,
                temperature=0.1,
                forced_provider=None,
            )

        self.assertTrue(client._provider_is_temporarily_disabled("provider-0"))


if __name__ == "__main__":
    unittest.main()
