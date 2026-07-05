from dataclasses import dataclass

from bot.config import Settings
from bot.utils.tokens import estimate_tokens


@dataclass(frozen=True)
class InputValidation:
    ok: bool
    message: str
    estimated_tokens: int


class MessageValidator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(self, text: str, prompt_text: str = "") -> InputValidation:
        if not text.strip():
            return InputValidation(False, "پیام خالی قابل ارسال نیست.", 0)
        if len(text) > self.settings.max_message_chars:
            return InputValidation(
                False,
                f"پیامت خیلی طولانی است. حداکثر {self.settings.max_message_chars} کاراکتر مجاز است.",
                0,
            )
        estimated = estimate_tokens(text) + estimate_tokens(prompt_text)
        estimated_total = estimated + self.settings.groq_max_completion_tokens
        if estimated_total > self.settings.max_request_tokens:
            return InputValidation(
                False,
                (
                    "این پیام از سقف توکن مجاز بیشتر می‌شود و اجازه ارسال ندارد.\n"
                    f"حداکثر: {self.settings.max_request_tokens} توکن\n"
                    f"تخمین این درخواست: {estimated_total} توکن"
                ),
                estimated_total,
            )
        return InputValidation(True, "", estimated_total)
