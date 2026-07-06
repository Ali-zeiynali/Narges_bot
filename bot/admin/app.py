from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from bot.admin.services import AdminDataService, compact_number, dt_iso, mask_key
from bot.config import Settings, load_settings
from bot.logging_config import setup_logging
from bot.services.group_service import send_messages, send_messages_detailed
from bot.storage.database import Database


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["dt"] = dt_iso
templates.env.filters["mask_key"] = mask_key
templates.env.filters["compact_number"] = compact_number
templates.env.filters["json_pretty"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2, default=str)


def create_admin_app(settings: Settings | None = None, database: Database | None = None, route_prefix: str = "/admin") -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_file, settings.log_level)
    database = database or Database(settings.database_path)
    database.migrate()
    service = AdminDataService(database, settings)
    app = FastAPI(title="Narges Admin", docs_url=None, redoc_url=None)

    route_prefix = route_prefix.rstrip("/")

    def route(path: str) -> str:
        if not path:
            return route_prefix or "/"
        return f"{route_prefix}{path}" if route_prefix else path

    def render(request: Request, template: str, context: dict[str, Any] | None = None) -> HTMLResponse:
        context = context or {}
        context.update({"request": request, "settings": settings, "flash": request.query_params.get("flash")})
        return templates.TemplateResponse(request, template, context)

    def is_authenticated(request: Request) -> bool:
        token = settings.admin_panel_token
        if not token:
            return request.client is not None and request.client.host in {"127.0.0.1", "::1", "localhost"}
        supplied = request.cookies.get("admin_token") or request.headers.get("x-admin-token") or request.query_params.get("token")
        return supplied == token

    def require_admin(request: Request) -> None:
        if not is_authenticated(request):
            raise HTTPException(status_code=303, headers={"Location": "/admin/login"})

    @app.get("/", include_in_schema=False)
    async def root(request: Request) -> Response:
        if route_prefix:
            return RedirectResponse(route_prefix)
        require_admin(request)
        return render(request, "dashboard.html", {"snapshot": service.dashboard()})

    @app.get(route("/login"), response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        return render(request, "login.html", {"local_bypass": not settings.admin_panel_token})

    @app.post(route("/login"))
    async def login(request: Request) -> RedirectResponse:
        form = await request.form()
        token = str(form.get("token", ""))
        if settings.admin_panel_token and token != settings.admin_panel_token:
            return RedirectResponse("/admin/login?flash=توکن نامعتبر است", status_code=303)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie("admin_token", token, httponly=True, samesite="lax")
        return response

    @app.get(route("/logout"))
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie("admin_token")
        return response

    @app.get(route(""), response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "dashboard.html", {"snapshot": service.dashboard()})

    @app.get(route("/users"), response_class=HTMLResponse)
    async def users(request: Request, sort: str = "last_seen", q: str = "") -> HTMLResponse:
        require_admin(request)
        return render(request, "users.html", {"users": service.users(sort=sort, query=q), "sort": sort, "q": q})

    @app.get(route("/users/{user_id}"), response_class=HTMLResponse)
    async def user_detail(request: Request, user_id: int) -> HTMLResponse:
        require_admin(request)
        detail = service.user_detail(user_id)
        if not detail["user"]:
            return render(request, "message.html", {"title": "کاربر پیدا نشد", "message": "این کاربر در دیتابیس وجود ندارد."})
        return render(request, "user_detail.html", {"detail": detail, "user_id": user_id})

    @app.get(route("/messages"), response_class=HTMLResponse)
    async def messages(request: Request, user_id: str | None = None, limit: int = 300) -> HTMLResponse:
        require_admin(request)
        parsed_user_id = int(user_id) if user_id and user_id.lstrip("-").isdigit() else None
        return render(request, "messages.html", service.messages(user_id=parsed_user_id, limit=limit))

    @app.post(route("/users/{user_id}/extra"))
    async def add_user_extra(request: Request, user_id: int) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        amount = int(str(form.get("amount", "0")).strip() or "0")
        reason = str(form.get("reason", "manual")).strip()
        service.add_user_extra_credit(user_id, amount, reason)
        return RedirectResponse(f"/admin/users/{user_id}?flash=extra اضافه شد", status_code=303)

    @app.post(route("/users/{user_id}/memories"))
    async def add_user_memory(request: Request, user_id: int) -> RedirectResponse:
        require_admin(request)
        service.add_memory_from_form(user_id, dict(await request.form()))
        return RedirectResponse(f"/admin/users/{user_id}?flash=حافظه اضافه شد", status_code=303)

    @app.post(route("/users/{user_id}/memories/{memory_id}/delete"))
    async def delete_user_memory(request: Request, user_id: int, memory_id: int) -> RedirectResponse:
        require_admin(request)
        service.delete_memory(user_id, memory_id)
        return RedirectResponse(f"/admin/users/{user_id}?flash=حافظه حذف شد", status_code=303)

    @app.post(route("/users/{user_id}/warnings"))
    async def add_user_warning(request: Request, user_id: int) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        service.add_user_warning(user_id, str(form.get("reason", "")).strip())
        return RedirectResponse(f"/admin/users/{user_id}?flash=اخطار اضافه شد", status_code=303)

    @app.post(route("/users/{user_id}/warnings/{warning_id}/delete"))
    async def delete_user_warning(request: Request, user_id: int, warning_id: int) -> RedirectResponse:
        require_admin(request)
        service.delete_user_warning(user_id, warning_id)
        return RedirectResponse(f"/admin/users/{user_id}?flash=اخطار حذف شد", status_code=303)

    @app.post(route("/users/{user_id}/delete"))
    async def delete_user(request: Request, user_id: int) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        ok = service.delete_user_completely(user_id, str(form.get("confirm", "")))
        if not ok:
            return RedirectResponse(f"/admin/users/{user_id}?flash=برای حذف کامل، آیدی کاربر را دقیق وارد کن", status_code=303)
        return RedirectResponse("/admin/users?flash=کاربر کامل حذف شد", status_code=303)

    @app.get(route("/messages/{message_id}"), response_class=HTMLResponse)
    async def message_detail(request: Request, message_id: int) -> HTMLResponse:
        require_admin(request)
        return render(request, "message_detail.html", service.message_detail(message_id))

    @app.get(route("/invoices"), response_class=HTMLResponse)
    async def invoices(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "invoices.html", service.invoices())

    @app.get(route("/model"), response_class=HTMLResponse)
    async def model_state(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "model.html", service.model_state())

    @app.post(route("/model/state"))
    async def save_model_state(request: Request) -> RedirectResponse:
        require_admin(request)
        form = dict(await request.form())
        ok, message = service.save_state_from_form(form)
        return RedirectResponse(f"/admin/model?flash={message}", status_code=303)

    @app.post(route("/model/ai"))
    async def save_ai_toggle(request: Request) -> RedirectResponse:
        require_admin(request)
        service.save_ai_toggle_from_form(dict(await request.form()))
        return RedirectResponse("/admin/model?flash=وضعیت اتصال هوش مصنوعی ذخیره شد", status_code=303)

    @app.get(route("/providers"), response_class=HTMLResponse)
    async def providers(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "providers.html", {"providers": service.provider_config(), "statuses": service.provider_statuses()})

    @app.post(route("/providers/{provider_name}"))
    async def update_provider(request: Request, provider_name: str) -> RedirectResponse:
        require_admin(request)
        service.update_provider(provider_name, dict(await request.form()))
        return RedirectResponse("/admin/providers?flash=provider ذخیره شد", status_code=303)

    @app.post(route("/providers/{provider_name}/keys"))
    async def add_provider_key(request: Request, provider_name: str) -> RedirectResponse:
        require_admin(request)
        return RedirectResponse("/admin/providers?flash=ویرایش کلید از پنل غیرفعال است", status_code=303)

    @app.post(route("/providers/{provider_name}/keys/{key_index}/delete"))
    async def delete_provider_key(request: Request, provider_name: str, key_index: int) -> RedirectResponse:
        require_admin(request)
        return RedirectResponse("/admin/providers?flash=ویرایش کلید از پنل غیرفعال است", status_code=303)

    @app.get(route("/broadcast"), response_class=HTMLResponse)
    async def broadcast_page(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(
            request,
            "broadcast.html",
            {
                "logs": service.logs()["broadcasts"],
                "groups": service.group_chats(),
                "schedules": service.scheduled_group_messages(),
            },
        )

    @app.post(route("/broadcast"))
    async def broadcast_send(request: Request) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        text = str(form.get("text", "")).strip()
        media_type = str(form.get("media_type", "text")).strip()
        media_file = form.get("media_file")
        target_type = str(form.get("target_type", "users")).strip()
        target_value = str(form.get("target_value", "")).strip() or None
        only_ready = str(form.get("only_ready", "on")).lower() in {"on", "true", "1"}
        has_file = hasattr(media_file, "filename") and bool(getattr(media_file, "filename", ""))
        if not text and not has_file:
            return RedirectResponse("/admin/broadcast?flash=متن پیام خالی است", status_code=303)
        if target_type == "groups":
            target_ids = service.target_group_ids()
        elif target_type == "user":
            if not target_value or not target_value.lstrip("-").isdigit():
                return RedirectResponse("/admin/broadcast?flash=شناسه کاربر معتبر نیست", status_code=303)
            target_ids = [int(target_value)]
        else:
            target_type = "users"
            target_ids = service.target_user_ids(only_ready=only_ready)
        broadcast_id = service.create_broadcast(
            text or f"[{media_type}]",
            len(target_ids),
            target_type=target_type,
            target_value=target_value,
        )
        file_payload = None
        if has_file:
            content = await media_file.read()  # type: ignore[attr-defined]
            file_payload = (getattr(media_file, "filename", "broadcast.bin") or "broadcast.bin", content)
        sent, failed, error, deliveries = await send_broadcast(settings, target_ids, text, detailed=True, media_type=media_type, file_payload=file_payload)
        service.finish_broadcast(broadcast_id, sent, failed, error, deliveries=deliveries)
        return RedirectResponse(f"/admin/broadcast?flash=ارسال تمام شد: {sent} موفق، {failed} ناموفق", status_code=303)

    @app.get(route("/broadcast/{broadcast_id}"), response_class=HTMLResponse)
    async def broadcast_detail(request: Request, broadcast_id: int) -> HTMLResponse:
        require_admin(request)
        return render(request, "broadcast_detail.html", service.broadcast_detail(broadcast_id))

    @app.post(route("/broadcast/schedules"))
    async def create_group_schedule(request: Request) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if not text:
            return RedirectResponse("/admin/broadcast?flash=متن زمان‌بندی خالی است", status_code=303)
        service.create_scheduled_group_message(
            text=text,
            interval_minutes=int(str(form.get("interval_minutes", "180")).strip() or "180"),
            enabled=str(form.get("enabled", "on")).lower() in {"on", "true", "1"},
        )
        return RedirectResponse("/admin/broadcast?flash=زمان‌بندی گروهی ذخیره شد", status_code=303)

    @app.post(route("/broadcast/schedules/{schedule_id}"))
    async def update_group_schedule(request: Request, schedule_id: int) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        service.update_scheduled_group_message(
            schedule_id,
            str(form.get("text", "")).strip(),
            int(str(form.get("interval_minutes", "180")).strip() or "180"),
            str(form.get("enabled", "")).lower() in {"on", "true", "1"},
        )
        return RedirectResponse("/admin/broadcast?flash=زمان‌بندی بروزرسانی شد", status_code=303)

    @app.post(route("/broadcast/schedules/{schedule_id}/delete"))
    async def delete_group_schedule(request: Request, schedule_id: int) -> RedirectResponse:
        require_admin(request)
        service.delete_scheduled_group_message(schedule_id)
        return RedirectResponse("/admin/broadcast?flash=زمان‌بندی حذف شد", status_code=303)

    @app.get(route("/channels"), response_class=HTMLResponse)
    async def channels_page(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "channels.html", {"channels": service.channels()})

    @app.post(route("/channels"))
    async def create_channel(request: Request) -> RedirectResponse:
        require_admin(request)
        service.save_channel_from_form(dict(await request.form()))
        return RedirectResponse("/admin/channels?flash=کانال ذخیره شد", status_code=303)

    @app.post(route("/channels/{channel_id}"))
    async def update_channel(request: Request, channel_id: int) -> RedirectResponse:
        require_admin(request)
        service.update_channel_from_form(channel_id, dict(await request.form()))
        return RedirectResponse("/admin/channels?flash=کانال بروزرسانی شد", status_code=303)

    @app.post(route("/channels/{channel_id}/delete"))
    async def delete_channel(request: Request, channel_id: int) -> RedirectResponse:
        require_admin(request)
        service.channel_service.remove_channel(0, channel_id)
        return RedirectResponse("/admin/channels?flash=کانال حذف شد", status_code=303)

    @app.get(route("/memories"), response_class=HTMLResponse)
    async def memories_page(request: Request, user_id: int | None = None) -> HTMLResponse:
        require_admin(request)
        return render(request, "memories.html", service.memories(user_id=user_id))

    @app.get(route("/logs"), response_class=HTMLResponse)
    async def logs(request: Request, kind: str = "debug") -> HTMLResponse:
        require_admin(request)
        return render(request, "logs.html", service.logs(kind=kind))

    return app


async def send_broadcast(settings: Settings, user_ids: list[int], text: str, detailed: bool = False, media_type: str = "text", file_payload: tuple[str, bytes] | None = None):
    from bot.telegram_session import create_telegram_session

    session = create_telegram_session(settings.telegram_proxy)
    bot = Bot(token=settings.telegram_token, session=session)
    try:
        if detailed:
            if file_payload and media_type in {"photo", "voice"}:
                from bot.services.group_service import MessageDeliveryResult

                deliveries = []
                filename, content = file_payload
                for user_id in user_ids:
                    try:
                        file = BufferedInputFile(content, filename=filename)
                        if media_type == "photo":
                            sent_message = await bot.send_photo(user_id, file, caption=text or None)
                        else:
                            sent_message = await bot.send_voice(user_id, file, caption=text or None)
                        deliveries.append(MessageDeliveryResult(target_id=user_id, status="sent", telegram_message_id=sent_message.message_id))
                    except Exception as exc:
                        deliveries.append(MessageDeliveryResult(target_id=user_id, status="failed", error=f"{exc.__class__.__name__}: {exc}"))
            else:
                deliveries = await send_messages_detailed(bot, user_ids, text)
            sent = sum(1 for item in deliveries if item.status == "sent")
            failed = len(deliveries) - sent
            first_error = next((item.error for item in deliveries if item.error), None)
            return sent, failed, first_error, deliveries
        return await send_messages(bot, user_ids, text)
    finally:
        await bot.session.close()


app = create_admin_app()
