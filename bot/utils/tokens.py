import math


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))
