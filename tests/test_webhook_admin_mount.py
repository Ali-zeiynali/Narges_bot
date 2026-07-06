import importlib
from pathlib import Path


def test_webhook_mounts_admin_panel(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "dummy")
    monkeypatch.setenv("GROQ_API_KEY", "dummy")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "narges.sqlite3"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "bot.log"))
    monkeypatch.setenv("AI_PROVIDERS_CONFIG", str(Path("config/ai_providers.json")))

    webhook = importlib.import_module("bot.webhook")
    paths = [(type(route).__name__, getattr(route, "path", None)) for route in webhook.app.routes]

    assert ("Mount", "/admin") in paths
