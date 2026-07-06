import os

from bot.config import load_settings
from bot.logging_config import setup_logging


def main() -> None:
    import uvicorn

    settings = load_settings()
    setup_logging(settings.log_file, settings.log_level)
    uvicorn.run(
        "bot.webhook:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
