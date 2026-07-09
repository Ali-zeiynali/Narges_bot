from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
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
    database = database or Database(settings.database_url or settings.database_path)
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
    async def users(request: Request, sort: str = "last_seen", q: str = "", active_only: str = "", ready_only: str = "") -> HTMLResponse:
        require_admin(request)
        only_active = active_only.lower() in {"1", "true", "on", "yes"}
        only_ready = ready_only.lower() in {"1", "true", "on", "yes"}
        return render(
            request,
            "users.html",
            {
                "users": service.users(sort=sort, query=q, active_only=only_active, ready_only=only_ready),
                "sort": sort,
                "q": q,
                "active_only": only_active,
                "ready_only": only_ready,
            },
        )

    @app.get(route("/users/{user_id}"), response_class=HTMLResponse)
    async def user_detail(request: Request, user_id: int) -> HTMLResponse:
        require_admin(request)
        detail = service.user_detail(user_id)
        if not detail["user"]:
            return render(request, "message.html", {"title": "کاربر پیدا نشد", "message": "این کاربر در دیتابیس وجود ندارد."})
        return render(request, "user_detail.html", {"detail": detail, "user_id": user_id})

    @app.post(route("/users/{user_id}/send"))
    async def send_user_message(request: Request, user_id: int) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        text = str(form.get("text", "")).strip()
        media_type = str(form.get("media_type", "text")).strip()
        media_file = form.get("media_file")
        has_file = hasattr(media_file, "filename") and bool(getattr(media_file, "filename", ""))
        if not text and not has_file:
            return RedirectResponse(f"/admin/users/{user_id}?flash=متن پیام خالی است", status_code=303)
        file_payload = None
        if has_file:
            content = await media_file.read()  # type: ignore[attr-defined]
            file_payload = (getattr(media_file, "filename", "admin-message.bin") or "admin-message.bin", content)
        _sent, failed, error, deliveries = await send_broadcast(
            settings,
            [user_id],
            text,
            detailed=True,
            media_type=media_type,
            file_payload=file_payload,
            target_type="user",
        )
        delivery = deliveries[0] if deliveries else None
        if failed or error or delivery is None or delivery.status != "sent":
            failure_reason = error or (delivery.error if delivery else None) or "delivery failed"
            return RedirectResponse(f"/admin/users/{user_id}?flash=ارسال ناموفق: {failure_reason}", status_code=303)
        service.record_admin_direct_message(user_id, text or f"[{media_type}]", delivery.telegram_message_id)
        service.record_admin_uploaded_media(
            target_id=user_id,
            target_type="user",
            text=text,
            media_type=media_type,
            file_payload=file_payload,
            telegram_message_id=delivery.telegram_message_id,
        )
        return RedirectResponse(f"/admin/users/{user_id}?flash=پیام از طرف ربات ارسال شد", status_code=303)

    @app.get(route("/messages"), response_class=HTMLResponse)
    async def messages(request: Request, user_id: str | None = None, limit: int = 300) -> HTMLResponse:
        require_admin(request)
        parsed_user_id = int(user_id) if user_id and user_id.lstrip("-").isdigit() else None
        return render(request, "messages.html", service.messages(user_id=parsed_user_id, limit=limit))

    @app.get(route("/group-messages"), response_class=HTMLResponse)
    async def group_messages(request: Request, chat_id: str | None = None, section: str = "all", limit: int = 300) -> HTMLResponse:
        require_admin(request)
        parsed_chat_id = int(chat_id) if chat_id and chat_id.lstrip("-").isdigit() else None
        return render(request, "group_messages.html", service.group_messages(chat_id=parsed_chat_id, section=section, limit=limit))

    @app.post(route("/group-messages/send"))
    async def send_group_message(request: Request) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        chat_id_raw = str(form.get("chat_id", "")).strip() or str(form.get("manual_chat_id", "")).strip()
        text = str(form.get("text", "")).strip()
        if not chat_id_raw.lstrip("-").isdigit():
            return RedirectResponse("/admin/group-messages?flash=شناسه گروه معتبر نیست", status_code=303)
        chat_id = int(chat_id_raw)
        if not text:
            return RedirectResponse(f"/admin/group-messages?chat_id={chat_id}&flash=متن پیام خالی است", status_code=303)
        telegram_message_id, error = await send_direct_text(settings, chat_id, text)
        if error:
            return RedirectResponse(f"/admin/group-messages?chat_id={chat_id}&flash=ارسال ناموفق: {error}", status_code=303)
        service.record_admin_group_message(chat_id, text, telegram_message_id)
        return RedirectResponse(f"/admin/group-messages?chat_id={chat_id}&section=scheduled&flash=پیام گروهی ارسال شد", status_code=303)

    @app.get(route("/media"), response_class=HTMLResponse)
    async def media_page(request: Request, user_id: str | None = None, kind: str = "", q: str = "", limit: int = 240) -> HTMLResponse:
        require_admin(request)
        parsed_user_id = int(user_id) if user_id and user_id.lstrip("-").isdigit() else None
        return render(request, "media.html", service.media(user_id=parsed_user_id, kind=kind, q=q, limit=limit))

    @app.get(route("/media/{media_id}/file"))
    async def media_file(request: Request, media_id: int) -> Response:
        require_admin(request)
        media = service.media_file(media_id)
        if media is None:
            raise HTTPException(status_code=404)
        payload, mime_type = service.media_file_payload(media_id)
        if payload:
            return Response(content=payload, media_type=mime_type or media.mime_type or "application/octet-stream")
        path = Path(media.storage_path)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type=media.mime_type or "application/octet-stream")

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

    @app.post(route("/invoices/{invoice_id}/approve"))
    async def approve_invoice(request: Request, invoice_id: str) -> RedirectResponse:
        require_admin(request)
        ok, invoice, reason = service.review_invoice(invoice_id, approve=True)
        if ok and invoice:
            await notify_invoice_result(settings, invoice.user_id, True, invoice.message_quota)
        flash = "پرداخت تایید شد" if ok else f"تایید نشد: {reason}"
        return RedirectResponse(f"/admin/invoices?flash={flash}", status_code=303)

    @app.post(route("/invoices/{invoice_id}/reject"))
    async def reject_invoice(request: Request, invoice_id: str) -> RedirectResponse:
        require_admin(request)
        ok, invoice, reason = service.review_invoice(invoice_id, approve=False)
        if invoice:
            await notify_invoice_result(settings, invoice.user_id, False, invoice.message_quota)
        flash = "پرداخت رد شد" if invoice else f"رد نشد: {reason}"
        return RedirectResponse(f"/admin/invoices?flash={flash}", status_code=303)

    @app.get(route("/backup"), response_class=HTMLResponse)
    async def backup_page(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "backup.html")

    @app.get(route("/backup/export"))
    async def backup_export(request: Request) -> Response:
        require_admin(request)
        payload = service.export_backup()
        content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        filename = f"narges-backup-{payload['created_at'][:10]}.json"
        return Response(
            content,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post(route("/backup/import"))
    async def backup_import(request: Request) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        file = form.get("backup_file")
        if not hasattr(file, "read"):
            return RedirectResponse("/admin/backup?flash=فایل JSON انتخاب نشده", status_code=303)
        try:
            payload = json.loads((await file.read()).decode("utf-8-sig"))
            report = service.import_backup(payload)
        except Exception as exc:
            return RedirectResponse(f"/admin/backup?flash=Import ناموفق: {exc}", status_code=303)
        return RedirectResponse(
            f"/admin/backup?flash=Import تمام شد: {report['inserted']} اضافه، {report['skipped']} رد شد",
            status_code=303,
        )

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
        form = await request.form()
        service.add_provider_key(provider_name, str(form.get("api_key", "")).strip())
        return RedirectResponse("/admin/providers?flash=کلید provider اضافه شد", status_code=303)

    @app.post(route("/providers/{provider_name}/keys/{key_index}/delete"))
    async def delete_provider_key(request: Request, provider_name: str, key_index: int) -> RedirectResponse:
        require_admin(request)
        service.delete_provider_key(provider_name, key_index)
        return RedirectResponse("/admin/providers?flash=کلید provider حذف شد", status_code=303)

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
        active_within_raw = str(form.get("active_within_hours", "")).strip()
        active_within_hours = int(active_within_raw) if active_within_raw.isdigit() and int(active_within_raw) > 0 else None
        has_file = hasattr(media_file, "filename") and bool(getattr(media_file, "filename", ""))
        if not text and not has_file:
            return RedirectResponse("/admin/broadcast?flash=متن پیام خالی است", status_code=303)
        if target_type == "groups":
            target_ids = service.target_group_ids()
            user_target_ids: list[int] = []
            group_target_ids = target_ids
        elif target_type == "all":
            user_target_ids = service.target_user_ids(only_ready=only_ready, active_within_hours=active_within_hours)
            group_target_ids = service.target_group_ids()
            target_ids = user_target_ids + group_target_ids
        elif target_type == "user":
            if not target_value or not target_value.lstrip("-").isdigit():
                return RedirectResponse("/admin/broadcast?flash=شناسه کاربر معتبر نیست", status_code=303)
            target_ids = [int(target_value)]
            user_target_ids = target_ids
            group_target_ids = []
        elif target_type == "group":
            if not target_value or not target_value.lstrip("-").isdigit():
                return RedirectResponse("/admin/broadcast?flash=شناسه گروه معتبر نیست", status_code=303)
            target_ids = [int(target_value)]
            user_target_ids = []
            group_target_ids = target_ids
        else:
            target_type = "users"
            target_ids = service.target_user_ids(only_ready=only_ready, active_within_hours=active_within_hours)
            user_target_ids = target_ids
            group_target_ids = []
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
        if target_type == "all":
            sent_users, failed_users, error_users, user_deliveries = await send_broadcast(
                settings,
                user_target_ids,
                text,
                detailed=True,
                media_type=media_type,
                file_payload=file_payload,
                target_type="user",
            )
            sent_groups, failed_groups, error_groups, group_deliveries = await send_broadcast(
                settings,
                group_target_ids,
                text,
                detailed=True,
                media_type=media_type,
                file_payload=file_payload,
                target_type="group",
            )
            sent = sent_users + sent_groups
            failed = failed_users + failed_groups
            error = error_users or error_groups
            deliveries = user_deliveries + group_deliveries
        else:
            delivery_target_type = "group" if target_type in {"groups", "group"} else "user"
            sent, failed, error, deliveries = await send_broadcast(
                settings,
                target_ids,
                text,
                detailed=True,
                media_type=media_type,
                file_payload=file_payload,
                target_type=delivery_target_type,
            )
        service.finish_broadcast(broadcast_id, sent, failed, error, deliveries=deliveries)
        if target_type in {"user", "all"}:
            for delivery in deliveries:
                if delivery.status == "sent" and delivery.target_type == "user":
                    service.record_admin_direct_message(
                        delivery.target_id,
                        text or f"[{media_type}]",
                        delivery.telegram_message_id,
                    )
                    service.record_admin_uploaded_media(
                        target_id=delivery.target_id,
                        target_type="user",
                        text=text,
                        media_type=media_type,
                        file_payload=file_payload,
                        telegram_message_id=delivery.telegram_message_id,
                    )
        if target_type in {"groups", "group", "all"}:
            for delivery in deliveries:
                if delivery.status == "sent" and delivery.target_type == "group":
                    service.record_admin_group_message(delivery.target_id, text or f"[{media_type}]", delivery.telegram_message_id)
                    service.record_admin_uploaded_media(
                        target_id=delivery.target_id,
                        target_type="group",
                        text=text,
                        media_type=media_type,
                        file_payload=file_payload,
                        telegram_message_id=delivery.telegram_message_id,
                    )
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
    async def logs(request: Request, kind: str = "debug", user_id: str | None = None, page: int = 1, limit: int = 100) -> HTMLResponse:
        require_admin(request)
        parsed_user_id = int(user_id) if user_id and user_id.lstrip("-").isdigit() else None
        return render(request, "logs.html", service.logs(kind=kind, user_id=parsed_user_id, page=page, limit=limit))

    return app


async def send_broadcast(
    settings: Settings,
    user_ids: list[int],
    text: str,
    detailed: bool = False,
    media_type: str = "text",
    file_payload: tuple[str, bytes] | None = None,
    target_type: str = "user",
):
    from bot.telegram_session import create_telegram_session

    session = create_telegram_session(settings.telegram_proxy)
    bot = Bot(token=settings.telegram_token, session=session)
    try:
        if detailed:
            if file_payload and media_type in {"photo", "voice", "document"}:
                from bot.services.group_service import MessageDeliveryResult

                deliveries = []
                filename, content = file_payload
                for user_id in user_ids:
                    try:
                        file = BufferedInputFile(content, filename=filename)
                        if media_type == "photo":
                            sent_message = await bot.send_photo(user_id, file, caption=text or None)
                        elif media_type == "voice":
                            sent_message = await bot.send_voice(user_id, file, caption=text or None)
                        else:
                            sent_message = await bot.send_document(user_id, file, caption=text or None)
                        deliveries.append(
                            MessageDeliveryResult(
                                target_id=user_id,
                                status="sent",
                                telegram_message_id=sent_message.message_id,
                                target_type=target_type,
                            )
                        )
                    except Exception as exc:
                        deliveries.append(
                            MessageDeliveryResult(
                                target_id=user_id,
                                status="failed",
                                error=f"{exc.__class__.__name__}: {exc}",
                                target_type=target_type,
                            )
                        )
            else:
                deliveries = await send_messages_detailed(bot, user_ids, text, target_type=target_type)
            sent = sum(1 for item in deliveries if item.status == "sent")
            failed = len(deliveries) - sent
            first_error = next((item.error for item in deliveries if item.error), None)
            return sent, failed, first_error, deliveries
        return await send_messages(bot, user_ids, text)
    finally:
        await bot.session.close()


async def send_direct_text(settings: Settings, chat_id: int, text: str) -> tuple[int | None, str | None]:
    from bot.telegram_session import create_telegram_session

    session = create_telegram_session(settings.telegram_proxy)
    bot = Bot(token=settings.telegram_token, session=session)
    try:
        message = await bot.send_message(chat_id, text)
        return int(message.message_id), None
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    finally:
        await bot.session.close()


async def notify_invoice_result(settings: Settings, user_id: int, approved: bool, message_quota: int) -> None:
    from bot.telegram_session import create_telegram_session

    session = create_telegram_session(settings.telegram_proxy)
    bot = Bot(token=settings.telegram_token, session=session)
    try:
        if approved:
            text = (
                "✅ پرداختت تایید شد\n\n"
                f"🎉 {message_quota} پیام به ظرفیتت اضافه شد.\n"
                "از الان می‌تونی استفاده کنی."
            )
        else:
            text = (
                "❌ پرداخت تایید نشد\n\n"
                "رسید یا مبلغ با فاکتور هم‌خوانی نداشت.\n"
                "اگر فکر می‌کنی اشتباه شده، با پشتیبانی پیام بده."
            )
        await bot.send_message(user_id, text)
    finally:
        await bot.session.close()


app = create_admin_app()
