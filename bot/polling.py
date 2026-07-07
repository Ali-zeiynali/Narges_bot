import asyncio
import logging
import time
from datetime import UTC

from aiogram.types import Update

from bot.application import create_bot_application


logger = logging.getLogger(__name__)


def _actor_key(update: Update) -> tuple[int, int] | None:
    message = update.message
    if not message or not message.from_user:
        return None
    return (message.chat.id, message.from_user.id)


def _is_offline_backlog(update: Update, startup_unix_time: float, grace_seconds: int) -> bool:
    message = update.message
    if not message:
        return False
    message_time = message.date.astimezone(UTC).timestamp()
    return message_time < startup_unix_time - max(0, grace_seconds)


def _latest_backlog_updates(
    updates: list[Update],
    *,
    startup_unix_time: float,
    grace_seconds: int,
    latest_only: bool,
) -> list[Update]:
    if not latest_only:
        return updates

    latest_by_actor: dict[tuple[int, int], int] = {}
    for update in updates:
        key = _actor_key(update)
        if key is None or not _is_offline_backlog(update, startup_unix_time, grace_seconds):
            continue
        update_id = int(update.update_id)
        previous = latest_by_actor.get(key)
        if previous is None or update_id > previous:
            latest_by_actor[key] = update_id

    selected: list[Update] = []
    for update in updates:
        key = _actor_key(update)
        if key is None or not _is_offline_backlog(update, startup_unix_time, grace_seconds):
            selected.append(update)
            continue
        if latest_by_actor.get(key) == int(update.update_id):
            selected.append(update)
        else:
            logger.info("polling_skipped_stale_backlog update_id=%s", update.update_id)
    return selected


async def _drain_startup_backlog(app, startup_unix_time: float) -> None:
    settings = app.settings
    allowed_updates = app.dispatcher.resolve_used_update_types()
    offset: int | None = None
    updates: list[Update] = []

    while True:
        batch = await app.bot.get_updates(
            offset=offset,
            limit=100,
            timeout=0,
            allowed_updates=allowed_updates,
        )
        if not batch:
            break
        updates.extend(batch)
        offset = int(batch[-1].update_id) + 1
        if len(batch) < 100:
            break
    if offset is not None:
        await app.bot.get_updates(
            offset=offset,
            limit=1,
            timeout=0,
            allowed_updates=allowed_updates,
        )

    selected = _latest_backlog_updates(
        updates,
        startup_unix_time=startup_unix_time,
        grace_seconds=settings.telegram_backlog_grace_seconds,
        latest_only=settings.telegram_backlog_latest_only,
    )
    for update in selected:
        await app.dispatcher.feed_update(app.bot, update)
    if updates:
        logger.info("polling_startup_backlog_drained total=%s processed=%s", len(updates), len(selected))


async def run_polling() -> None:
    startup_unix_time = time.time()
    app = create_bot_application()
    await app.startup()
    try:
        await app.bot.delete_webhook(drop_pending_updates=False)
        await _drain_startup_backlog(app, startup_unix_time)
        await app.dispatcher.start_polling(
            app.bot,
            allowed_updates=app.dispatcher.resolve_used_update_types(),
        )
    finally:
        await app.shutdown()


def main() -> None:
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
