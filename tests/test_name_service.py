import unittest

from bot.services.name_service import NameService


class NameServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = NameService({"ali": "علی"})

    def test_rejects_latin_name_even_when_transliteration_exists(self) -> None:
        result = self.service.validate("Ali")

        self.assertFalse(result.ok)
        self.assertIn("فارسی", result.reason or "")

    def test_rejects_url_username_and_emoji_only(self) -> None:
        self.assertFalse(self.service.validate("@promo_channel").ok)
        self.assertFalse(self.service.validate("https://example.com").ok)
        self.assertFalse(self.service.validate("😀").ok)

    def test_allows_persian_name(self) -> None:
        result = self.service.validate("آرتام")

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized, "آرتام")

    def test_rejects_unmapped_latin_name(self) -> None:
        result = self.service.validate("Daria")

        self.assertFalse(result.ok)
        self.assertIn("فارسی", result.reason or "")


if __name__ == "__main__":
    unittest.main()
