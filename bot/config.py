import os
from dataclasses import dataclass
import json

from dotenv import load_dotenv


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required. Check your .env file.")
    return value


def int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


def float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number.") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}.")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_proxy: str | None
    groq_proxy: str | None
    groq_api_key: str
    groq_model: str
    groq_temperature: float
    groq_max_completion_tokens: int
    max_request_tokens: int
    max_message_chars: int
    persona_version: str
    database_path: str
    log_file: str
    log_level: str
    admin_ids: tuple[int, ...]
    support_url: str | None
    free_daily_quota: int
    free_monthly_quota: int
    rate_limit_short_count: int
    rate_limit_short_window_seconds: int
    rate_limit_long_count: int
    rate_limit_long_window_seconds: int
    membership_cache_seconds: int
    admin_bypass_minutes: int
    debug_mode: bool
    debug_user_ids: tuple[int, ...]
    name_transliteration_map: dict[str, str]
    max_api_input_tokens: int = 3000
    ai_providers_config: str = "config/ai_providers.json"
    admin_panel_token: str | None = None
    admin_panel_host: str = "127.0.0.1"
    admin_panel_port: int = 8080
    reengagement_enabled: bool = True
    reengagement_after_hours: int = 30
    reengagement_message: str = "دلم برات تنگ شدهه🥺 کجایی"
    reengagement_check_seconds: int = 600


def csv_int_env(name: str) -> tuple[int, ...]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return ()
    values: list[int] = []
    for item in raw_value.split(","):
        item = item.strip()
        if item:
            values.append(int(item))
    return tuple(values)


def json_map_env(name: str, default: dict[str, str]) -> dict[str, str]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object.")
    return {str(key).strip().lower(): str(value).strip() for key, value in parsed.items()}


def bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    return raw_value in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv()
    proxy = os.getenv("TELEGRAM_PROXY", "http://127.0.0.1:12334").strip()
    groq_proxy = os.getenv("GROQ_PROXY", "").strip() or proxy
    default_name_map = {
        "ali": "علی",
        "mohammad": "محمد",
        "mohamad": "محمد",
        "reza": "رضا",
        "sara": "سارا",
        "zahra": "زهرا",
        "fateme": "فاطمه",
        "narges": "نرگس",
    }
    return Settings(
        telegram_token=require_env("TELEGRAM_TOKEN"),
        telegram_proxy=proxy or None,
        groq_proxy=groq_proxy or None,
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        groq_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip(),
        groq_temperature=float_env("GROQ_TEMPERATURE", 0.7, 0, 2),
        groq_max_completion_tokens=int_env("GROQ_MAX_COMPLETION_TOKENS", 512, 1, 4096),
        max_request_tokens=min(int_env("MAX_REQUEST_TOKENS", 3000, 128, 131072), 3000),
        max_api_input_tokens=min(int_env("MAX_API_INPUT_TOKENS", 2400, 128, 131072), 2400),
        max_message_chars=int_env("MAX_MESSAGE_CHARS", 1800, 1, 4096),
        persona_version=os.getenv("PERSONA_VERSION", "2026-07-05.1").strip(),
        database_path=os.getenv("DATABASE_PATH", "data/narges.sqlite3").strip(),
        log_file=os.getenv("LOG_FILE", "logs/bot.log").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        admin_ids=csv_int_env("ADMIN_IDS"),
        support_url=os.getenv("SUPPORT_URL", "").strip() or None,
        free_daily_quota=int_env("FREE_DAILY_QUOTA", 40, 1, 10000),
        free_monthly_quota=int_env("FREE_MONTHLY_QUOTA", 300, 1, 100000),
        rate_limit_short_count=int_env("RATE_LIMIT_SHORT_COUNT", 6, 1, 100),
        rate_limit_short_window_seconds=int_env("RATE_LIMIT_SHORT_WINDOW_SECONDS", 120, 10, 3600),
        rate_limit_long_count=int_env("RATE_LIMIT_LONG_COUNT", 15, 1, 300),
        rate_limit_long_window_seconds=int_env("RATE_LIMIT_LONG_WINDOW_SECONDS", 600, 60, 86400),
        membership_cache_seconds=int_env("MEMBERSHIP_CACHE_SECONDS", 60, 5, 3600),
        admin_bypass_minutes=int_env("ADMIN_BYPASS_MINUTES", 60, 1, 10080),
        debug_mode=bool_env("DEBUG_MODE", False),
        debug_user_ids=csv_int_env("DEBUG_USER_IDS"),
        name_transliteration_map=json_map_env("NAME_TRANSLITERATION_MAP", default_name_map),
        ai_providers_config=os.getenv("AI_PROVIDERS_CONFIG", "config/ai_providers.json").strip(),
        admin_panel_token=os.getenv("ADMIN_PANEL_TOKEN", "").strip() or None,
        admin_panel_host=os.getenv("ADMIN_PANEL_HOST", "127.0.0.1").strip(),
        admin_panel_port=int_env("ADMIN_PANEL_PORT", 8080, 1, 65535),
        reengagement_enabled=bool_env("REENGAGEMENT_ENABLED", True),
        reengagement_after_hours=int_env("REENGAGEMENT_AFTER_HOURS", 30, 1, 720),
        reengagement_message=os.getenv("REENGAGEMENT_MESSAGE", "دلم برات تنگ شدهه🥺 کجایی").strip() or "دلم برات تنگ شدهه🥺 کجایی",
        reengagement_check_seconds=int_env("REENGAGEMENT_CHECK_SECONDS", 600, 60, 86400),
    )
