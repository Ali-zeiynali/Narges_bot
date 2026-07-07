from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

import httpx
from aiogram import Bot
from aiogram.types import Message
from sqlalchemy import func, select

from bot.config import Settings
from bot.storage.database import Database
from bot.storage.orm import MediaFileORM


logger = logging.getLogger(__name__)


VISION_ERROR_MESSAGE = "فعلا چشمام درد می‌کنه! نمی‌تونم ببینم."
UNSUPPORTED_AUDIO_MESSAGE = "فعلا هندزفری ندارم که اینارو ببینم!"
UNSUPPORTED_MEDIA_MESSAGE = "نفهمیدم چی فرستادی. فعلا فقط متن و عکس رو می‌فهمم."

SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

SAFE_MEDIA_SUFFIXES = {
    ".bin",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".mp4",
    ".mov",
    ".webm",
    ".mp3",
    ".wav",
    ".ogg",
    ".oga",
    ".m4a",
    ".pdf",
    ".txt",
}


class MediaStorageError(RuntimeError):
    pass


class VisionProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredMedia:
    id: int
    user_id: int
    chat_id: int | None
    telegram_message_id: int | None
    telegram_file_id: str
    media_kind: str
    mime_type: str | None
    original_file_name: str | None
    storage_path: str
    file_size: int | None
    caption: str | None
    created_at: datetime


@dataclass(frozen=True)
class VisionProviderConfig:
    name: str
    kind: str
    base_url: str
    model: str
    api_keys: tuple[str, ...]
    enabled: bool = True
    priority: int | None = None
    temperature: float = 0.2
    max_tokens: int = 260
    timeout_seconds: float = 45
    use_proxy: bool = True
    extra_headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None


class MediaStorageService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.storage_dir = Path(settings.media_storage_dir)

    async def store_photo(self, bot: Bot, message: Message) -> StoredMedia:
        if not message.from_user or not message.photo:
            raise MediaStorageError("photo message is incomplete")
        photo = message.photo[-1]
        return await self._store_telegram_file(
            bot=bot,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            telegram_file_id=photo.file_id,
            media_kind="image",
            mime_type="image/jpeg",
            original_file_name=None,
            caption=message.caption,
            reported_file_size=photo.file_size,
            max_bytes=self.settings.max_image_file_bytes,
        )

    async def store_document(self, bot: Bot, message: Message) -> StoredMedia:
        if not message.from_user or not message.document:
            raise MediaStorageError("document message is incomplete")
        document = message.document
        file_name = self._safe_original_name(document.file_name)
        mime_type = (document.mime_type or mimetypes.guess_type(file_name or "")[0] or "application/octet-stream").lower()
        media_kind = "image" if mime_type in SUPPORTED_IMAGE_MIME_TYPES else "document"
        max_bytes = self.settings.max_image_file_bytes if media_kind == "image" else self.settings.max_media_file_bytes
        return await self._store_telegram_file(
            bot=bot,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            telegram_file_id=document.file_id,
            media_kind=media_kind,
            mime_type=mime_type,
            original_file_name=file_name,
            caption=message.caption,
            reported_file_size=document.file_size,
            max_bytes=max_bytes,
        )

    async def store_unsupported_media(self, bot: Bot, message: Message) -> StoredMedia:
        user_id = message.from_user.id if message.from_user else 0
        media = message.voice or message.video or message.audio or message.animation or message.video_note or message.sticker
        if not user_id or media is None:
            raise MediaStorageError("unsupported media message is incomplete")
        media_kind = self._media_kind(message)
        mime_type = getattr(media, "mime_type", None)
        original_file_name = self._safe_original_name(getattr(media, "file_name", None))
        return await self._store_telegram_file(
            bot=bot,
            user_id=user_id,
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            telegram_file_id=media.file_id,
            media_kind=media_kind,
            mime_type=mime_type,
            original_file_name=original_file_name,
            caption=message.caption,
            reported_file_size=getattr(media, "file_size", None),
            max_bytes=self.settings.max_media_file_bytes,
        )

    def image_count_today(self, user_id: int) -> int:
        since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count())
                .select_from(MediaFileORM)
                .where(
                    MediaFileORM.user_id == user_id,
                    MediaFileORM.media_kind == "image",
                    MediaFileORM.created_at >= since,
                )
            )
        return int(value or 0)

    def is_supported_image(self, media: StoredMedia) -> bool:
        return media.media_kind == "image" and (media.mime_type or "").lower() in SUPPORTED_IMAGE_MIME_TYPES

    def set_vision_description(self, media_id: int, description: str) -> None:
        with self.database.orm.session() as session:
            row = session.get(MediaFileORM, media_id)
            if row is None:
                return
            metadata = self._loads_metadata(row.metadata_json)
            metadata["vision_description"] = description.strip()
            metadata["vision_described_at"] = datetime.now(UTC).isoformat()
            row.metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)

    async def _store_telegram_file(
        self,
        *,
        bot: Bot,
        user_id: int,
        chat_id: int | None,
        telegram_message_id: int | None,
        telegram_file_id: str,
        media_kind: str,
        mime_type: str | None,
        original_file_name: str | None,
        caption: str | None,
        reported_file_size: int | None,
        max_bytes: int,
    ) -> StoredMedia:
        if reported_file_size is not None and reported_file_size > max_bytes:
            raise MediaStorageError("media file is too large")
        telegram_file = await bot.get_file(telegram_file_id)
        file_size = telegram_file.file_size or reported_file_size
        if file_size is not None and file_size > max_bytes:
            raise MediaStorageError("media file is too large")
        suffix = self._suffix_for(media_kind, mime_type, original_file_name, telegram_file.file_path)
        created_at = datetime.now(UTC)
        target_dir = self.storage_dir / str(user_id) / created_at.date().isoformat()
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid.uuid4().hex}{suffix}"
        await bot.download_file(telegram_file.file_path, destination=target_path)
        if not target_path.exists() or target_path.stat().st_size <= 0:
            raise MediaStorageError("telegram file download failed")
        if target_path.stat().st_size > max_bytes:
            target_path.unlink(missing_ok=True)
            raise MediaStorageError("downloaded media file is too large")
        return self._record(
            user_id=user_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            telegram_file_id=telegram_file_id,
            media_kind=media_kind,
            mime_type=mime_type,
            original_file_name=original_file_name,
            storage_path=str(target_path.resolve()),
            file_size=target_path.stat().st_size,
            caption=caption,
            metadata={
                "telegram_file_path": telegram_file.file_path,
                "reported_file_size": reported_file_size,
            },
            created_at=created_at,
        )

    def _record(
        self,
        *,
        user_id: int,
        chat_id: int | None,
        telegram_message_id: int | None,
        telegram_file_id: str,
        media_kind: str,
        mime_type: str | None,
        original_file_name: str | None,
        storage_path: str,
        file_size: int | None,
        caption: str | None,
        metadata: dict[str, Any],
        created_at: datetime,
    ) -> StoredMedia:
        with self.database.orm.session() as session:
            row = MediaFileORM(
                user_id=user_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                telegram_file_id=telegram_file_id,
                media_kind=media_kind,
                mime_type=mime_type,
                original_file_name=original_file_name,
                storage_path=storage_path,
                file_size=file_size,
                caption=caption,
                metadata_json=json.dumps(metadata, ensure_ascii=False, default=str),
                created_at=created_at,
            )
            session.add(row)
            session.flush()
            return StoredMedia(
                id=int(row.id),
                user_id=row.user_id,
                chat_id=row.chat_id,
                telegram_message_id=row.telegram_message_id,
                telegram_file_id=row.telegram_file_id,
                media_kind=row.media_kind,
                mime_type=row.mime_type,
                original_file_name=row.original_file_name,
                storage_path=row.storage_path,
                file_size=row.file_size,
                caption=row.caption,
                created_at=row.created_at,
            )

    def _loads_metadata(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return value if isinstance(value, dict) else {"value": value}

    def _suffix_for(self, media_kind: str, mime_type: str | None, original_file_name: str | None, telegram_file_path: str | None) -> str:
        normalized_mime = (mime_type or "").lower()
        if media_kind == "image" and normalized_mime in SUPPORTED_IMAGE_MIME_TYPES:
            return SUPPORTED_IMAGE_MIME_TYPES[normalized_mime]
        suffix = Path(original_file_name or telegram_file_path or "").suffix.lower()
        return suffix if suffix in SAFE_MEDIA_SUFFIXES else ".bin"

    def _safe_original_name(self, value: str | None) -> str | None:
        if not value:
            return None
        name = PurePath(value).name.strip().replace("\x00", "")
        return name[:180] or None

    def _media_kind(self, message: Message) -> str:
        if message.voice:
            return "voice"
        if message.video:
            return "video"
        if message.audio:
            return "audio"
        if message.animation:
            return "animation"
        if message.video_note:
            return "video_note"
        if message.sticker:
            return "sticker"
        return "media"


class VisionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.config_path = Path(settings.vision_providers_config)
        self.proxy_url = settings.groq_proxy or settings.telegram_proxy
        self.http_client = httpx.Client(proxy=self.proxy_url, timeout=45) if self.proxy_url else httpx.Client(timeout=45)
        self.direct_http_client = httpx.Client(timeout=45)
        self._providers_fingerprint: tuple[int, int] | None = None
        self.providers = self._load_providers()

    def describe_image(self, media: StoredMedia) -> str:
        if not media.mime_type or media.mime_type.lower() not in SUPPORTED_IMAGE_MIME_TYPES:
            raise VisionProviderError("unsupported image mime type")
        image_path = Path(media.storage_path)
        if not image_path.exists() or image_path.stat().st_size <= 0:
            raise VisionProviderError("stored image is missing")
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data_url = f"data:{media.mime_type};base64,{image_b64}"
        prompt = (
            "این تصویر را برای استفاده در یک گفتگوی فارسی توصیف کن. "
            "فقط JSON معتبر با کلید description بده. "
            "توضیح باید دقیق، کوتاه، عینی و بدون حدس‌های حساس درباره هویت، دین، نژاد یا سلامت افراد باشد."
        )
        last_error: Exception | None = None
        self._reload_if_changed()
        for provider in self.providers:
            if not provider.enabled:
                continue
            for api_key in self._resolve_keys(provider):
                try:
                    return self._request_provider(provider, api_key, prompt, data_url)
                except Exception as exc:
                    last_error = exc
                    logger.warning("vision_provider_failed provider=%s error=%s", provider.name, exc)
        raise VisionProviderError(str(last_error or "no vision provider is available"))

    def _request_provider(self, provider: VisionProviderConfig, api_key: str, prompt: str, data_url: str) -> str:
        if provider.kind != "openai_compatible":
            raise VisionProviderError(f"unsupported provider type: {provider.kind}")
        base_url = self._normalized_openai_base_url(provider.base_url)
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": "You describe images for a Persian Telegram chat. Return only JSON."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            "temperature": provider.temperature,
            "max_tokens": provider.max_tokens,
            "stream": False,
            **(provider.extra_body or {}),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(provider.extra_headers or {}),
        }
        response = self._client_for(provider).post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=provider.timeout_seconds,
        )
        if response.status_code >= 400:
            raise VisionProviderError(f"HTTP {response.status_code}: {response.text[:400]}")
        data = response.json()
        raw_text = self._extract_text(data)
        description = self._description_from_text(raw_text)
        if not description:
            raise VisionProviderError("empty vision description")
        return description[:1200]

    def _load_providers(self) -> list[VisionProviderConfig]:
        if not self.config_path.exists():
            raise RuntimeError(f"Vision providers config not found: {self.config_path}")
        raw_text = self.config_path.read_text(encoding="utf-8-sig")
        stat = self.config_path.stat()
        self._providers_fingerprint = (stat.st_mtime_ns, hash(raw_text))
        raw = json.loads(raw_text)
        providers = raw.get("providers", raw if isinstance(raw, list) else [])
        configured = [
            self._provider_from_dict(item, index)
            for index, item in enumerate(providers)
            if isinstance(item, dict)
        ]
        ordered = sorted(
            enumerate(configured),
            key=lambda pair: pair[1].priority if pair[1].priority is not None else 10_000 + pair[0],
        )
        return [provider for _index, provider in ordered]

    def _reload_if_changed(self) -> None:
        if not self.config_path.exists():
            return
        raw_text = self.config_path.read_text(encoding="utf-8-sig")
        stat = self.config_path.stat()
        fingerprint = (stat.st_mtime_ns, hash(raw_text))
        if fingerprint != self._providers_fingerprint:
            self.providers = self._load_providers()

    def _provider_from_dict(self, item: dict[str, Any], index: int) -> VisionProviderConfig:
        api_keys = list(item.get("api_keys", []))
        if item.get("api_key_env"):
            api_keys.insert(0, f"env:{item['api_key_env']}")
        model = item.get("model")
        if not model and isinstance(item.get("models"), list) and item["models"]:
            model = item["models"][0]
        return VisionProviderConfig(
            name=str(item["name"]).strip(),
            kind=str(item.get("type") or item.get("kind") or "openai_compatible").strip(),
            base_url=str(item.get("base_url") or "").strip(),
            model=str(model or "").strip(),
            api_keys=tuple(str(value).strip() for value in api_keys if str(value).strip()),
            enabled=bool(item.get("enabled", True)),
            priority=int(item["priority"]) if item.get("priority") is not None else index,
            temperature=float(item.get("temperature", 0.2)),
            max_tokens=int(item.get("max_tokens", item.get("max_completion_tokens", 260))),
            timeout_seconds=float(item.get("timeout_seconds", 45)),
            use_proxy=bool(item.get("use_proxy", True)),
            extra_headers=dict(item.get("extra_headers") or {}),
            extra_body=dict(item.get("extra_body") or {}),
        )

    def _resolve_keys(self, provider: VisionProviderConfig) -> list[str]:
        keys: list[str] = []
        for raw in provider.api_keys:
            if raw.startswith("env:"):
                value = os.getenv(raw[4:], "").strip()
                if value:
                    keys.append(value)
            elif raw.startswith("$"):
                value = os.getenv(raw[1:], "").strip()
                if value:
                    keys.append(value)
            elif raw:
                keys.append(raw)
        return keys

    def _client_for(self, provider: VisionProviderConfig) -> httpx.Client:
        return self.http_client if provider.use_proxy else self.direct_http_client

    def _normalized_openai_base_url(self, base_url: str) -> str:
        value = (base_url or "").rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if value.endswith(suffix):
                value = value[: -len(suffix)].rstrip("/")
        if not value:
            raise VisionProviderError("base_url is required")
        return value

    def _extract_text(self, data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict)).strip()
        return ""

    def _description_from_text(self, raw_text: str) -> str:
        text = (raw_text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            value = payload.get("description") or payload.get("text") or payload.get("caption")
            return str(value or "").strip()
        return text
