from aiogram.client.session.aiohttp import AiohttpSession


def create_telegram_session(proxy_url: str | None) -> AiohttpSession:
    if proxy_url:
        return AiohttpSession(proxy=proxy_url)
    return AiohttpSession()
