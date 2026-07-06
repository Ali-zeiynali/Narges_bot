import uvicorn

from bot.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "bot.admin.app:app",
        host=settings.admin_panel_host,
        port=settings.admin_panel_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
