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
