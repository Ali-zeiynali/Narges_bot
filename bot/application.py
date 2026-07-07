import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher

from bot.config import Settings, load_settings
from bot.handlers import register_handlers
from bot.logging_config import setup_logging
from bot.persona.compiler import PersonaCompiler
from bot.services.billing_service import BillingService
from bot.services.chat_service import ChatService
from bot.services.context_builder import ContextBuilder
from bot.services.conversation_search_tool import ConversationSearchTool
from bot.services.debug_service import DebugService
from bot.services.global_state_service import GlobalStateService
from bot.services.group_service import GroupMessageScheduler, GroupService
from bot.services.groq_client import GroqChatClient
from bot.services.history_service import HistoryService
from bot.services.media_service import MediaStorageService, VisionClient
from bot.services.memory_service import MemoryService
from bot.services.menu_service import MenuService
from bot.services.moderation_service import ModerationService
from bot.services.name_service import NameService
from bot.services.narges_state_scheduler import NargesStateScheduler
from bot.services.narges_state_service import NargesStateService
from bot.services.quota_service import QuotaService
from bot.services.reengagement_service import ReengagementScheduler, ReengagementService
from bot.services.required_channel_service import RequiredChannelService
from bot.services.style_linter import StyleLinter
from bot.services.usage_service import UsageService
from bot.services.user_service import UserService
from bot.services.validation import MessageValidator
from bot.storage.database import Database
from bot.telegram_session import create_telegram_session


logger = logging.getLogger(__name__)


@dataclass
class BotApplication:
    settings: Settings
    database: Database
    bot: Bot
    dispatcher: Dispatcher
    menu_service: MenuService
    narges_state_scheduler: NargesStateScheduler
    group_service: GroupService
    reengagement_service: ReengagementService
    background_tasks: list[asyncio.Task] = field(default_factory=list)

    async def startup(self) -> None:
        await self.menu_service.setup_commands(self.bot)
        self.background_tasks = [
            asyncio.create_task(self.narges_state_scheduler.run_forever(), name="narges-state-scheduler"),
            asyncio.create_task(GroupMessageScheduler(self.group_service, self.bot).run_forever(), name="group-scheduler"),
        ]
        if self.settings.reengagement_enabled:
            self.background_tasks.append(
                asyncio.create_task(ReengagementScheduler(self.reengagement_service, self.bot).run_forever(), name="reengagement-scheduler")
            )
        logger.info(
            "bot_application_started model=%s persona_version=%s proxy_enabled=%s",
            self.settings.groq_model,
            self.settings.persona_version,
            bool(self.settings.telegram_proxy),
        )

    async def shutdown(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        for task in self.background_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.bot.session.close()
        logger.info("bot_application_stopped")


def create_bot_application(settings: Settings | None = None) -> BotApplication:
    settings = settings or load_settings()
    setup_logging(settings.log_file, settings.log_level)

    database = Database(settings.database_url or settings.database_path)
    database.migrate()

    debug_service = DebugService(database, settings)
    billing_service = BillingService(database)
    memory_service = MemoryService(database, debug_service=debug_service)
    moderation_service = ModerationService(database, debug_service=debug_service)
    quota_service = QuotaService(database, settings, debug_service=debug_service)
    menu_service = MenuService(settings)
    channel_service = RequiredChannelService(database, settings.membership_cache_seconds, settings.admin_ids)
    group_service = GroupService(database)
    user_service = UserService(database)
    name_service = NameService(settings.name_transliteration_map)
    groq_client = GroqChatClient(settings, database)
    narges_state_service = NargesStateService(database)
    global_state_service = GlobalStateService(database)
    narges_state_scheduler = NargesStateScheduler(narges_state_service, groq_client)
    history_service = HistoryService(database)
    media_storage_service = MediaStorageService(settings, database)
    vision_client = VisionClient(settings)
    context_builder = ContextBuilder(database, history_service)
    chat_service = ChatService(
        validator=MessageValidator(settings),
        persona_compiler=PersonaCompiler(settings.persona_version),
        groq_client=groq_client,
        narges_state_service=narges_state_service,
        memory_service=memory_service,
        history_service=history_service,
        context_builder=context_builder,
        conversation_search_tool=ConversationSearchTool(history_service),
        moderation_service=moderation_service,
        debug_service=debug_service,
        usage_service=UsageService(database, settings.groq_model),
        style_linter=StyleLinter(),
        quota_service=quota_service,
        global_state_service=global_state_service,
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
        history_service=history_service,
        narges_state_service=narges_state_service,
        debug_service=debug_service,
        group_service=group_service,
        media_storage_service=media_storage_service,
        vision_client=vision_client,
        settings=settings,
    )

    session = create_telegram_session(settings.telegram_proxy)
    bot = Bot(token=settings.telegram_token, session=session)

    return BotApplication(
        settings=settings,
        database=database,
        bot=bot,
        dispatcher=dispatcher,
        menu_service=menu_service,
        narges_state_scheduler=narges_state_scheduler,
        group_service=group_service,
        reengagement_service=ReengagementService(database, settings),
    )
