import unittest

from bot.services.style_linter import StyleLinter


class StyleLinterTests(unittest.TestCase):
    def test_detects_cliche_and_many_questions(self) -> None:
        result = StyleLinter().lint(
            ["کاملاً درک می‌کنم. خوبی؟ چی شد؟ چرا؟"],
            [],
        )

        self.assertTrue(result.serious)
        self.assertGreaterEqual(len(result.issues), 2)

    def test_accepts_simple_natural_reply(self) -> None:
        result = StyleLinter().lint(["باشه، کوتاه و روشن می‌گم."], [])

        self.assertFalse(result.serious)


if __name__ == "__main__":
    unittest.main()
