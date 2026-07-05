import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from bot.config import load_settings
from bot.handlers import register_handlers
from bot.logging_config import setup_logging
from bot.persona.compiler import PersonaCompiler
from bot.services.chat_service import ChatService
from bot.services.billing_service import BillingService
from bot.services.groq_client import GroqChatClient
from bot.services.history_service import HistoryService
from bot.services.conversation_search_tool import ConversationSearchTool
from bot.services.debug_service import DebugService
from bot.services.memory_service import MemoryService
from bot.services.menu_service import MenuService
from bot.services.moderation_service import ModerationService
from bot.services.name_service import NameService
from bot.services.quota_service import QuotaService
from bot.services.required_channel_service import RequiredChannelService
from bot.services.relationship_service import RelationshipService
from bot.services.narges_state_scheduler import NargesStateScheduler
from bot.services.narges_state_service import NargesStateService
from bot.services.style_linter import StyleLinter
from bot.services.usage_service import UsageService
from bot.services.validation import MessageValidator
from bot.storage.database import Database
from bot.services.user_service import UserService


logger = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_file, settings.log_level)

    database = Database(settings.database_path)
    database.migrate()

    debug_service = DebugService(database, settings)
    billing_service = BillingService(database)
    memory_service = MemoryService(database, debug_service=debug_service)
    moderation_service = ModerationService(database, debug_service=debug_service)
    quota_service = QuotaService(database, settings, debug_service=debug_service)
    menu_service = MenuService(settings)
    channel_service = RequiredChannelService(database, settings.membership_cache_seconds, settings.admin_ids)
    user_service = UserService(database)
    name_service = NameService(settings.name_transliteration_map)
    groq_client = GroqChatClient(settings)
    narges_state_service = NargesStateService(database)
    narges_state_scheduler = NargesStateScheduler(narges_state_service, groq_client)
    history_service = HistoryService(database)
    relationship_service = RelationshipService(database)
    chat_service = ChatService(
        validator=MessageValidator(settings),
        persona_compiler=PersonaCompiler(settings.persona_version),
        groq_client=groq_client,
        narges_state_service=narges_state_service,
        memory_service=memory_service,
        relationship_service=relationship_service,
        history_service=history_service,
        conversation_search_tool=ConversationSearchTool(history_service),
        moderation_service=moderation_service,
        debug_service=debug_service,
        usage_service=UsageService(database, settings.groq_model),
        style_linter=StyleLinter(),
        quota_service=quota_service,
    )

    dispatcher = Dispatcher()
    register_handlers(
        dispatcher=dispatcher,
        chat_service=chat_service,
        memory_service=memory_service,
        user_service=user_service,
        channel_service=channel_service,
        name_service=name_service,
        menu_service=menu_service,
        quota_service=quota_service,
        billing_service=billing_service,
        moderation_service=moderation_service,
        relationship_service=relationship_service,
        history_service=history_service,
        narges_state_service=narges_state_service,
        debug_service=debug_service,
        settings=settings,
    )

    session = AiohttpSession(proxy=settings.telegram_proxy)
    bot = Bot(token=settings.telegram_token, session=session)
    await menu_service.setup_commands(bot)
    scheduler_task = asyncio.create_task(narges_state_scheduler.run_forever())

    logger.info(
        "bot_started model=%s persona_version=%s proxy_enabled=%s",
        settings.groq_model,
        settings.persona_version,
        bool(settings.telegram_proxy),
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await bot.session.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    asyncio.run(main())
