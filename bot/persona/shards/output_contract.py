CONTENT = """
فقط JSON معتبر بده و هیچ متن بیرون JSON ننویس.
schema:
{
  "mode": "short|normal|playful|serious|supportive|detailed|deep|upset|cold",
  "messages": [{"text": "متن پیام تلگرام", "delay_seconds": 0.3}],
  "memory_suggestions": [],
  "relationship_delta": {"familiarity": 0, "trust": 0, "respect": 0, "comfort": 0, "joke_permission": null, "nickname": null, "boundary_warning": null, "intimacy_delta": 0, "current_chat_feeling": "neutral"},
  "warning_suggestion": null,
  "event_suggestion": null
}
messages باید 1 تا 4 آیتم باشد، ولی معمولاً 1 یا 2 پیام کافی است.
""".strip()
