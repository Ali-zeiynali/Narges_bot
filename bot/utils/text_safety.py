import re


_REPEATED_CHAR_RE = re.compile(r"(.)\1{7,}", re.DOTALL)


def clamp_repeated_chars(text: str, limit: int = 7) -> str:
    if limit < 1:
        return text
    return _REPEATED_CHAR_RE.sub(lambda match: match.group(1) * limit, text or "")


def meaningful_length(text: str) -> int:
    return len((text or "").strip())
