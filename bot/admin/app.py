from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiogram import Bot
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from bot.admin.services import AdminDataService, dt_iso, mask_key
from bot.config import Settings, load_settings
from bot.logging_config import setup_logging
from bot.storage.database import Database


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["dt"] = dt_iso
templates.env.filters["mask_key"] = mask_key
templates.env.filters["json_pretty"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2, default=str)


def create_admin_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_file, settings.log_level)
    database = database or Database(settings.database_path)
    database.migrate()
    service = AdminDataService(database, settings)
    app = FastAPI(title="Narges Admin", docs_url=None, redoc_url=None)

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
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin")

    @app.get("/admin/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        return render(request, "login.html", {"local_bypass": not settings.admin_panel_token})

    @app.post("/admin/login")
    async def login(request: Request) -> RedirectResponse:
        form = await request.form()
        token = str(form.get("token", ""))
        if settings.admin_panel_token and token != settings.admin_panel_token:
            return RedirectResponse("/admin/login?flash=توکن نامعتبر است", status_code=303)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie("admin_token", token, httponly=True, samesite="lax")
        return response

    @app.get("/admin/logout")
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie("admin_token")
        return response

    @app.get("/admin", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "dashboard.html", {"snapshot": service.dashboard()})

    @app.get("/admin/users", response_class=HTMLResponse)
    async def users(request: Request, sort: str = "last_seen", q: str = "") -> HTMLResponse:
        require_admin(request)
        return render(request, "users.html", {"users": service.users(sort=sort, query=q), "sort": sort, "q": q})

    @app.get("/admin/users/{user_id}", response_class=HTMLResponse)
    async def user_detail(request: Request, user_id: int) -> HTMLResponse:
        require_admin(request)
        detail = service.user_detail(user_id)
        if not detail["user"]:
            return render(request, "message.html", {"title": "کاربر پیدا نشد", "message": "این کاربر در دیتابیس وجود ندارد."})
        return render(request, "user_detail.html", {"detail": detail, "user_id": user_id})

    @app.get("/admin/model", response_class=HTMLResponse)
    async def model_state(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "model.html", service.model_state())

    @app.post("/admin/model/state")
    async def save_model_state(request: Request) -> RedirectResponse:
        require_admin(request)
        form = dict(await request.form())
        ok, message = service.save_state_from_form(form)
        return RedirectResponse(f"/admin/model?flash={message}", status_code=303)

    @app.get("/admin/providers", response_class=HTMLResponse)
    async def providers(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "providers.html", {"providers": service.provider_config(), "statuses": service.provider_statuses()})

    @app.post("/admin/providers/{provider_name}")
    async def update_provider(request: Request, provider_name: str) -> RedirectResponse:
        require_admin(request)
        service.update_provider(provider_name, dict(await request.form()))
        return RedirectResponse("/admin/providers?flash=تنظیمات provider ذخیره شد", status_code=303)

    @app.post("/admin/providers/{provider_name}/keys")
    async def add_provider_key(request: Request, provider_name: str) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        key = str(form.get("key", "")).strip()
        if key:
            service.add_provider_key(provider_name, key)
        return RedirectResponse("/admin/providers?flash=کلید اضافه شد", status_code=303)

    @app.post("/admin/providers/{provider_name}/keys/{key_index}/delete")
    async def delete_provider_key(request: Request, provider_name: str, key_index: int) -> RedirectResponse:
        require_admin(request)
        service.delete_provider_key(provider_name, key_index)
        return RedirectResponse("/admin/providers?flash=کلید حذف شد", status_code=303)

    @app.get("/admin/broadcast", response_class=HTMLResponse)
    async def broadcast_page(request: Request) -> HTMLResponse:
        require_admin(request)
        return render(request, "broadcast.html", {"logs": service.logs()["broadcasts"]})

    @app.post("/admin/broadcast")
    async def broadcast_send(request: Request) -> RedirectResponse:
        require_admin(request)
        form = await request.form()
        text = str(form.get("text", "")).strip()
        only_ready = str(form.get("only_ready", "on")).lower() in {"on", "true", "1"}
        if not text:
            return RedirectResponse("/admin/broadcast?flash=متن پیام خالی است", status_code=303)
        target_ids = service.target_user_ids(only_ready=only_ready)
        broadcast_id = service.create_broadcast(text, len(target_ids))
        sent, failed, error = await send_broadcast(settings.telegram_token, target_ids, text)
        service.finish_broadcast(broadcast_id, sent, failed, error)
        return RedirectResponse(f"/admin/broadcast?flash=ارسال تمام شد: {sent} موفق، {failed} ناموفق", status_code=303)

    @app.get("/admin/logs", response_class=HTMLResponse)
    async def logs(request: Request, kind: str = "debug") -> HTMLResponse:
        require_admin(request)
        return render(request, "logs.html", service.logs(kind=kind))

    return app


async def send_broadcast(token: str, user_ids: list[int], text: str) -> tuple[int, int, str | None]:
    bot = Bot(token=token)
    sent = 0
    failed = 0
    first_error: str | None = None
    try:
        for user_id in user_ids:
            try:
                await bot.send_message(user_id, text)
                sent += 1
                await asyncio.sleep(0.04)
            except Exception as exc:
                failed += 1
                if first_error is None:
                    first_error = f"{exc.__class__.__name__}: {exc}"
    finally:
        await bot.session.close()
    return sent, failed, first_error


app = create_admin_app()
