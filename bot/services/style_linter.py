import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


REPEATED_PHRASES = [
    "می‌فهمم",
    "کاملاً درک می‌کنم",
    "به عنوان یک هوش مصنوعی",
    "امیدوارم کمکت کرده باشم",
    "اگر سوال دیگری داشتی",
]


@dataclass
class StyleLintResult:
    serious: bool
    issues: list[str] = field(default_factory=list)

    @property
    def feedback(self) -> str:
        return "؛ ".join(self.issues[:4])


class StyleLinter:
    def lint(self, messages: list[str], recent_replies: list[str]) -> StyleLintResult:
        joined = "\n".join(messages)
        issues: list[str] = []

        if self._has_repeated_phrase(joined):
            issues.append("عبارت تکراری یا کلیشه‌ای دارد")
        if self._similar_edges(messages):
            issues.append("شروع یا پایان پیام‌ها بیش از حد شبیه است")
        if self._too_many_questions(joined):
            issues.append("سؤال زیاد پرسیده شده")
        if self._too_many_emojis(joined):
            issues.append("ایموجی زیاد است")
        if self._has_extra_headers(joined):
            issues.append("تیتر یا قالب‌بندی اضافی دارد")
        if self._too_similar_to_recent(joined, recent_replies):
            issues.append("به پاسخ‌های اخیر بیش از حد شبیه است")
        if self._too_much_nickname(joined):
            issues.append("لقب یا خطاب مستقیم زیاد تکرار شده")

        serious = len(issues) >= 2 or any("هوش مصنوعی" in issue for issue in issues)
        return StyleLintResult(serious=serious, issues=issues)

    def _has_repeated_phrase(self, text: str) -> bool:
        return any(phrase in text for phrase in REPEATED_PHRASES)

    def _similar_edges(self, messages: list[str]) -> bool:
        if len(messages) < 2:
            return False
        starts = [message[:18] for message in messages if len(message) >= 18]
        ends = [message[-18:] for message in messages if len(message) >= 18]
        return len(starts) != len(set(starts)) or len(ends) != len(set(ends))

    def _too_many_questions(self, text: str) -> bool:
        return text.count("?") + text.count("؟") > 2

    def _too_many_emojis(self, text: str) -> bool:
        return len(re.findall(r"[\U0001F300-\U0001FAFF]", text)) > 3

    def _has_extra_headers(self, text: str) -> bool:
        return bool(re.search(r"(^|\n)\s{0,3}(#{1,6}|\*\*[^*]+\*\*)", text))

    def _too_similar_to_recent(self, text: str, recent_replies: list[str]) -> bool:
        return any(SequenceMatcher(None, text, recent).ratio() > 0.82 for recent in recent_replies[-5:])

    def _too_much_nickname(self, text: str) -> bool:
        words = ["عزیزم", "جان", "رفیق", "گلم"]
        return sum(text.count(word) for word in words) > 2
