import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from bot.admin.app import create_admin_app
from bot.application import BotApplication, create_bot_application
from bot.config import load_settings
from bot.update_queue import EnqueueStatus, InMemoryRateLimiter, TelegramUpdateQueue, UpdateIdempotencyStore
from bot.worker import TelegramUpdateWorker
from bot.services.request_trace import RequestTrace


logger = logging.getLogger(__name__)


class RuntimeState:
    app: BotApplication | None = None
    queue: TelegramUpdateQueue | None = None
    worker: TelegramUpdateWorker | None = None


state = RuntimeState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = load_settings()
    bot_app = create_bot_application(settings)
    queue = TelegramUpdateQueue(
        maxsize=settings.webhook_queue_maxsize,
        idempotency=UpdateIdempotencyStore(max_items=settings.webhook_idempotency_max_items),
        rate_limiter=InMemoryRateLimiter(
            limit=settings.webhook_rate_limit_count,
            window_seconds=settings.webhook_rate_limit_window_seconds,
        ),
        backlog_latest_only=settings.telegram_backlog_latest_only,
        backlog_grace_seconds=settings.telegram_backlog_grace_seconds,
    )
    worker = TelegramUpdateWorker(
        queue=queue,
        dispatcher=bot_app.dispatcher,
        bot=bot_app.bot,
        workers=settings.webhook_worker_count,
        backlog_debounce_seconds=settings.telegram_backlog_debounce_seconds,
    )

    state.app = bot_app
    state.queue = queue
    state.worker = worker

    await bot_app.startup()
    worker.start()
    await configure_telegram_webhook(bot_app)
    try:
        yield
    finally:
        await worker.stop()
        await bot_app.shutdown()


app = FastAPI(title="Narges Telegram Webhook", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    queue = state.queue
    return {"ok": True, "queue_size": queue.qsize() if queue else None}


@app.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    settings = state.app.settings if state.app else load_settings()
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    if state.queue is None:
        raise HTTPException(status_code=503, detail="queue is not ready")

    trace = RequestTrace("telegram_webhook")
    try:
        with trace.step("read_json"):
            payload = await request.json()
    except Exception:
        logger.warning("webhook_invalid_json")
        return JSONResponse({"ok": True, "status": EnqueueStatus.INVALID.value})
    if not isinstance(payload, dict):
        return JSONResponse({"ok": True, "status": EnqueueStatus.INVALID.value})

    with trace.step("enqueue"):
        result = await state.queue.enqueue(payload)
    trace_payload = trace.finish(update_id=result.update_id, status=result.status.value, phase="webhook")
    elapsed_ms = trace_payload["total_ms"]
    if state.app:
        state.app.debug_service.trace(
            "request_trace",
            trace_payload,
            user_id=result.user_id,
        )
    if elapsed_ms > 100:
        logger.warning("webhook_slow_enqueue update_id=%s elapsed_ms=%s status=%s", result.update_id, elapsed_ms, result.status.value)
    else:
        logger.info("webhook_enqueued update_id=%s elapsed_ms=%s status=%s", result.update_id, elapsed_ms, result.status.value)

    if result.status == EnqueueStatus.FULL:
        raise HTTPException(status_code=503, detail="update queue is full")
    return JSONResponse({"ok": True, "status": result.status.value})


app.mount("/admin", create_admin_app(route_prefix=""))


async def configure_telegram_webhook(bot_app: BotApplication) -> None:
    settings = bot_app.settings
    if not settings.webhook_base_url:
        logger.warning("webhook_base_url_missing set WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL before production traffic")
        return
    webhook_url = f"{settings.webhook_base_url.rstrip('/')}/webhook/telegram"
    await bot_app.bot.set_webhook(
        webhook_url,
        secret_token=settings.telegram_webhook_secret or None,
        drop_pending_updates=settings.telegram_drop_pending_updates,
        allowed_updates=bot_app.allowed_updates(),
    )
    logger.info("telegram_webhook_configured url=%s drop_pending_updates=%s", webhook_url, settings.telegram_drop_pending_updates)
