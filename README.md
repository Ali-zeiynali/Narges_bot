# Narges Bot

یک بات تلگرام ماژولار که هر پیام کاربر را با persona نرگس به Groq می‌فرستد، خروجی ساختاریافته می‌گیرد، سبک پاسخ را lint می‌کند، مصرف توکن را log می‌کند و حافظه/رابطه/وضعیت جهانی ساده نگه می‌دارد.

## اجرا

1. وابستگی‌ها:

```powershell
venv\Scripts\pip.exe install -r requirements.txt
```

2. فایل `.env` را از `.env.example` بساز و مقدارها را پر کن.

3. اجرا:

```powershell
venv\Scripts\python.exe -m bot.main
```

در شروع برنامه، migrationهای SQLite روی `DATABASE_PATH` اجرا می‌شوند.

## ساختار

- `bot/config.py`: تنظیمات env
- `bot/persona/`: shardهای persona، cache و `PersonaCompiler`
- `bot/models/`: مدل‌های Pydantic برای خروجی مدل، حافظه، رابطه و وضعیت جهانی
- `bot/services/`: سرویس‌های Groq، حافظه، رابطه، scheduler، validation، history، usage و style lint
- `bot/services/event_service.py`: ذخیره و خواندن رویدادهای روزانه مشترک نرگس
- `bot/storage/`: SQLite و migrations
- `bot/handlers.py`: فرمان‌ها و پیام‌های تلگرام

## فرمان‌های تلگرام

- `/memory`: نمایش خلاصه حافظه‌های فعال کاربر
- `/forget <id>`: حذف یک حافظه
- `/help`: راهنمای کوتاه

## نکته‌ها

مدل اجازه ذخیره مستقیم حافظه یا ساخت واقعیت جهانی ندارد. فقط پیشنهاد می‌دهد و بک‌اند آن را از نظر حساسیت، تکرار، تزریق پرامپت، اعتبار و محدودیت تعداد بررسی می‌کند.

## Render webhook deployment

The bot now runs in webhook mode only. The FastAPI route receives Telegram updates, puts them into an in-memory `asyncio.Queue`, and returns immediately. A background worker feeds queued updates into the existing aiogram dispatcher, so AI calls stay outside the webhook request path.

### File structure

- `bot/main.py`: production process entrypoint for Render (`python -m bot.main`)
- `bot/webhook.py`: FastAPI app, `/webhook/telegram`, `/health`, webhook registration
- `bot/update_queue.py`: in-memory queue, update-id idempotency, simple per-user limiter
- `bot/worker.py`: background update consumer that calls aiogram `Dispatcher.feed_update`
- `bot/application.py`: shared bot/service factory used by the worker
- `bot/handlers.py`: existing aiogram handlers, preserved

### Render settings

Use the included `render.yaml`, or configure manually:

```text
Build command: pip install -r requirements.txt
Start command: python -m bot.main
Health check path: /health
```

For SQLite persistence on Render, attach a persistent disk and keep all mutable files under the disk mount. The included `render.yaml` uses:

```text
Disk mount path: /var/data
DATABASE_PATH=/var/data/narges.sqlite3
LOG_FILE=/var/data/logs/bot.log
AI_PROVIDERS_CONFIG=/var/data/config/ai_providers.json
```

Without a Render disk, `data/narges.sqlite3` is stored on the service filesystem and can reset on redeploy. If you stay on a plan without disks, move the storage layer to an external database instead.

The admin panel is mounted on the same FastAPI service:

```text
https://your-service.onrender.com/admin
```

If `/admin` returns 404 on Render, verify the deployed commit includes `bot.webhook:app` with `Mount('/admin')` and that the service was redeployed from the updated `render.yaml`.

Required environment variables:

```text
TELEGRAM_TOKEN=...
GROQ_API_KEY=...
WEBHOOK_BASE_URL=https://your-service.onrender.com
TELEGRAM_WEBHOOK_SECRET=<long random string>
ADMIN_PANEL_TOKEN=<long random admin token>
TELEGRAM_PROXY=
```

Recommended free-tier defaults:

```text
WEBHOOK_WORKER_COUNT=1
WEBHOOK_QUEUE_MAXSIZE=200
WEBHOOK_RATE_LIMIT_COUNT=30
WEBHOOK_RATE_LIMIT_WINDOW_SECONDS=60
TELEGRAM_DROP_PENDING_UPDATES=false
```

Keep `WEBHOOK_WORKER_COUNT=1` on the free tier to avoid SQLite write contention and concurrent AI calls for the same process. The webhook returns `{"ok": true}` for duplicates, invalid payloads, and rate-limited updates so Telegram does not retry them into duplicate AI work.
