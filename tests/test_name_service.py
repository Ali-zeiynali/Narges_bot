import unittest

from bot.services.name_service import NameService


class NameServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = NameService({"ali": "علی"})

    def test_maps_common_configured_name(self) -> None:
        result = self.service.validate("Ali")

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized, "علی")

    def test_rejects_url_username_and_emoji_only(self) -> None:
        self.assertFalse(self.service.validate("@promo_channel").ok)
        self.assertFalse(self.service.validate("https://example.com").ok)
        self.assertFalse(self.service.validate("😀").ok)

    def test_allows_rare_real_looking_name(self) -> None:
        result = self.service.validate("آرتام")

        self.assertTrue(result.ok)
        self.assertEqual(result.normalized, "آرتام")

    def test_marks_unmapped_latin_name_ambiguous(self) -> None:
        result = self.service.validate("Daria")

        self.assertTrue(result.ok)
        self.assertTrue(result.ambiguous)


if __name__ == "__main__":
    unittest.main()
