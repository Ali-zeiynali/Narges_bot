import re
import unicodedata
from dataclasses import dataclass


BAD_WORDS = {"فحش", "لعنتی", "احمق", "کثافت"}
AD_WORDS = {"تبلیغ", "خرید", "فروش", "کانال", "پیج", "ارزان", "تخفیف"}


@dataclass(frozen=True)
class NameValidationResult:
    ok: bool
    normalized: str | None = None
    ambiguous: bool = False
    reason: str | None = None


class NameService:
    def __init__(self, transliteration_map: dict[str, str]) -> None:
        self.transliteration_map = {key.lower(): value for key, value in transliteration_map.items()}

    def suggest_from_telegram(self, first_name: str | None, username: str | None) -> str | None:
        source = first_name or username
        if not source:
            return None
        result = self.validate(source, allow_ambiguous=True)
        return result.normalized if result.ok else None

    def validate(self, raw_name: str, allow_ambiguous: bool = True) -> NameValidationResult:
        name = self.normalize(raw_name)
        if not name:
            return NameValidationResult(False, reason="نام خالی است.")
        if self._has_control(name):
            return NameValidationResult(False, reason="نام شامل کاراکتر نامعتبر است.")
        if self._has_url_or_username(name):
            return NameValidationResult(False, reason="نام نباید لینک یا username باشد.")
        if self._emoji_only(name):
            return NameValidationResult(False, reason="نام فقط ایموجی نباشد.")
        if self._looks_like_ad(name):
            return NameValidationResult(False, reason="نام حالت تبلیغاتی دارد.")
        if self._has_bad_word(name):
            return NameValidationResult(False, reason="این نام قابل ذخیره نیست.")
        if self._looks_random(name):
            return NameValidationResult(False, reason="نام شبیه رشته تصادفی است.")
        if not self._is_persian_name(name):
            return NameValidationResult(False, reason="اسم باید با حروف فارسی نوشته شود؛ مثلا: نرگس")
        if len(name) < 2 or len(name) > 32:
            return NameValidationResult(False, reason="نام باید بین ۲ تا ۳۲ کاراکتر باشد.")
        return NameValidationResult(True, normalized=name, ambiguous=False)

    def normalize(self, raw_name: str) -> str:
        name = unicodedata.normalize("NFKC", raw_name or "")
        name = re.sub(r"[\u200f\u202a-\u202e]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name

    def _has_control(self, name: str) -> bool:
        return any(unicodedata.category(char).startswith("C") and char != "\u200c" for char in name)

    def _has_url_or_username(self, name: str) -> bool:
        return bool(re.search(r"(https?://|www\.|t\.me/|@[\w_]{3,})", name, re.IGNORECASE))

    def _emoji_only(self, name: str) -> bool:
        chars = [char for char in name if not char.isspace()]
        if not chars:
            return False
        return all(unicodedata.category(char) in {"So", "Sk"} for char in chars)

    def _looks_like_ad(self, name: str) -> bool:
        lowered = name.lower()
        return any(word in lowered for word in AD_WORDS)

    def _has_bad_word(self, name: str) -> bool:
        lowered = name.lower()
        return any(word in lowered for word in BAD_WORDS)

    def _looks_random(self, name: str) -> bool:
        compact = re.sub(r"\s+", "", name)
        if len(compact) < 8:
            return False
        if re.fullmatch(r"[A-Za-z0-9_]+", compact):
            digits = sum(char.isdigit() for char in compact)
            vowels = sum(char.lower() in "aeiou" for char in compact)
            return digits >= 3 or vowels == 0
        return False

    def _is_persian_name(self, name: str) -> bool:
        if re.search(r"[A-Za-z0-9]", name):
            return False
        return bool(re.search(r"[\u0600-\u06FF]", name)) and bool(re.fullmatch(r"[\u0600-\u06FF\s\u200c-]+", name))
