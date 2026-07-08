from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    content_hash: str | None
    file_bytes: bytes | None
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


@dataclass(frozen=True)
class BotImageCatalogItem:
    id: str
    path: str
    description: str
    mime_type: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class BotImagePayload:
    image_id: str
    content: bytes
    mime_type: str
    filename: str
    description: str


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

    async def store_profile_photo(self, bot: Bot, user_id: int, photo: Any, position: int) -> StoredMedia:
        file_id = str(getattr(photo, "file_id", "") or "").strip()
        if not file_id:
            raise MediaStorageError("profile photo is missing file_id")
        return await self._store_telegram_file(
            bot=bot,
            user_id=user_id,
            chat_id=None,
            telegram_message_id=None,
            telegram_file_id=file_id,
            media_kind="profile_photo",
            mime_type="image/jpeg",
            original_file_name=None,
            caption=f"Telegram profile photo #{position + 1}",
            reported_file_size=getattr(photo, "file_size", None),
            max_bytes=self.settings.max_image_file_bytes,
            metadata_extra={
                "source": "telegram_user_profile_photo",
                "profile_photo_position": position,
                "telegram_file_unique_id": getattr(photo, "file_unique_id", None),
            },
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

    def cached_vision_description(self, media: StoredMedia) -> str | None:
        if not media.content_hash:
            return None
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MediaFileORM)
                .where(
                    MediaFileORM.content_hash == media.content_hash,
                    MediaFileORM.media_kind.in_(("image", "profile_photo")),
                    MediaFileORM.id != media.id,
                )
                .order_by(MediaFileORM.id.desc())
                .limit(20)
            ).all()
        for row in rows:
            metadata = self._loads_metadata(row.metadata_json)
            description = str(metadata.get("vision_description") or "").strip()
            if description:
                return description
        return None

    def profile_photos_recently_synced(self, user_id: int, hours: int = 24) -> bool:
        since = datetime.now(UTC) - timedelta(hours=hours)
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count())
                .select_from(MediaFileORM)
                .where(
                    MediaFileORM.user_id == user_id,
                    MediaFileORM.media_kind.in_(("profile_photo", "profile_photo_sync")),
                    MediaFileORM.created_at >= since,
                )
            )
        return int(value or 0) > 0

    def mark_profile_photos_synced(self, user_id: int, total_count: int) -> None:
        created_at = datetime.now(UTC)
        self._record(
            user_id=user_id,
            chat_id=None,
            telegram_message_id=None,
            telegram_file_id=f"profile_photo_sync:{user_id}:{created_at.timestamp()}",
            media_kind="profile_photo_sync",
            mime_type=None,
            original_file_name=None,
            storage_path="",
            content_hash=None,
            file_bytes=None,
            file_size=None,
            caption=None,
            metadata={
                "source": "telegram_user_profile_photo_sync",
                "total_count": total_count,
                "synced_at": created_at.isoformat(),
            },
            created_at=created_at,
        )

    def profile_photo_context(self, user_id: int, limit: int = 3) -> list[dict[str, Any]]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MediaFileORM)
                .where(MediaFileORM.user_id == user_id, MediaFileORM.media_kind == "profile_photo")
                .order_by(MediaFileORM.created_at.desc(), MediaFileORM.id.desc())
                .limit(60)
            ).all()
        by_image: dict[str, dict[str, Any]] = {}
        for row in rows:
            metadata = self._loads_metadata(row.metadata_json)
            description = str(metadata.get("vision_description") or "").strip()
            if not description:
                continue
            key = row.content_hash or row.telegram_file_id
            if key in by_image:
                continue
            position = metadata.get("profile_photo_position")
            try:
                position_value = int(position)
            except (TypeError, ValueError):
                position_value = 999
            by_image[key] = {
                "source": "telegram_user_profile_photo",
                "note": "This vision description is from one of the user's Telegram profile photos.",
                "profile_photo_number": position_value + 1 if position_value != 999 else None,
                "description": description[:1000],
                "media_id": row.id,
                "content_hash": row.content_hash,
                "described_at": metadata.get("vision_described_at"),
            }
        items = sorted(
            by_image.values(),
            key=lambda item: item["profile_photo_number"] if item["profile_photo_number"] is not None else 999,
        )
        return items[:limit]

    def file_payload(self, media_id: int) -> tuple[bytes | None, str | None]:
        with self.database.orm.session() as session:
            row = session.get(MediaFileORM, media_id)
            if row is None:
                return None, None
            if row.file_bytes:
                return bytes(row.file_bytes), row.mime_type
            if row.content_hash:
                duplicate = session.scalar(
                    select(MediaFileORM)
                    .where(MediaFileORM.content_hash == row.content_hash, MediaFileORM.file_bytes.is_not(None))
                    .order_by(MediaFileORM.id.asc())
                    .limit(1)
                )
                if duplicate and duplicate.file_bytes:
                    return bytes(duplicate.file_bytes), row.mime_type or duplicate.mime_type
            if row.storage_path:
                path = Path(row.storage_path)
                if path.exists() and path.is_file():
                    return path.read_bytes(), row.mime_type
        return None, None

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
        metadata_extra: dict[str, Any] | None = None,
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
        file_bytes = target_path.read_bytes()
        content_hash = hashlib.sha256(file_bytes).hexdigest()
        duplicate_id = self._duplicate_media_id(content_hash)
        target_path.unlink(missing_ok=True)
        metadata = {
            "telegram_file_path": telegram_file.file_path,
            "reported_file_size": reported_file_size,
            "stored_in_database": True,
            "content_hash": content_hash,
        }
        if metadata_extra:
            metadata.update(metadata_extra)
        if duplicate_id:
            metadata["duplicate_of_media_id"] = duplicate_id
        return self._record(
            user_id=user_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            telegram_file_id=telegram_file_id,
            media_kind=media_kind,
            mime_type=mime_type,
            original_file_name=original_file_name,
            storage_path="",
            content_hash=content_hash,
            file_bytes=file_bytes,
            file_size=len(file_bytes),
            caption=caption,
            metadata=metadata,
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
        content_hash: str | None,
        file_bytes: bytes | None,
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
                content_hash=content_hash,
                file_bytes=file_bytes,
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
                content_hash=row.content_hash,
                file_bytes=file_bytes,
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

    def _duplicate_media_id(self, content_hash: str) -> int | None:
        with self.database.orm.session() as session:
            row = session.scalar(
                select(MediaFileORM.id)
                .where(MediaFileORM.content_hash == content_hash)
                .order_by(MediaFileORM.id.asc())
                .limit(1)
            )
        return int(row) if row else None

    def _suffix_for(self, media_kind: str, mime_type: str | None, original_file_name: str | None, telegram_file_path: str | None) -> str:
        normalized_mime = (mime_type or "").lower()
        if normalized_mime in SUPPORTED_IMAGE_MIME_TYPES:
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


class BotImageCatalog:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.catalog_path = Path(getattr(settings, "bot_image_catalog_path", "images/catalog.json"))
        self.image_dir = Path(getattr(settings, "bot_image_dir", "images"))

    def items_for_model(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "description": item.description,
                "tags": list(item.tags),
            }
            for item in self.load_items()
        ]

    def has_image(self, image_id: str) -> bool:
        return any(item.id == image_id for item in self.load_items())

    def matching_bot_image(self, media: StoredMedia) -> dict[str, Any] | None:
        if not media.content_hash:
            return None
        with self.database.orm.session() as session:
            row = session.scalar(
                select(MediaFileORM)
                .where(
                    MediaFileORM.media_kind == "bot_image",
                    MediaFileORM.content_hash == media.content_hash,
                )
                .order_by(MediaFileORM.id.asc())
                .limit(1)
            )
        if row is None:
            return None
        metadata = self._loads_metadata(row.metadata_json)
        return {
            "id": row.id,
            "catalog_id": metadata.get("catalog_id"),
            "description": metadata.get("description") or row.caption or "",
            "content_hash": row.content_hash,
        }

    def payload(self, image_id: str) -> BotImagePayload | None:
        item = self._item_by_id(image_id)
        if item is None:
            return None
        db_payload = self._database_payload(item)
        if db_payload is not None:
            return db_payload
        path = self._resolve_path(item.path)
        if not path.exists() or not path.is_file():
            return None
        content = path.read_bytes()
        if not content:
            return None
        self._try_seed_item(item, content)
        return BotImagePayload(
            image_id=item.id,
            content=content,
            mime_type=item.mime_type,
            filename=path.name,
            description=item.description,
        )

    def record_sent_image(
        self,
        *,
        image_id: str,
        user_id: int,
        chat_id: int | None,
        telegram_message_id: int | None,
        caption: str | None,
    ) -> int | None:
        payload = self.payload(image_id)
        if payload is None or not payload.content:
            return None
        content_hash = hashlib.sha256(payload.content).hexdigest()
        item = self._item_by_id(image_id)
        created_at = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = MediaFileORM(
                user_id=user_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                telegram_file_id=f"local:{image_id}:sent:{telegram_message_id or created_at.timestamp()}",
                media_kind="bot_image",
                mime_type=payload.mime_type,
                original_file_name=payload.filename,
                storage_path=str(self._resolve_path(item.path)) if item else "",
                content_hash=content_hash,
                file_bytes=payload.content,
                file_size=len(payload.content),
                caption=caption,
                metadata_json=json.dumps(
                    {
                        "catalog_id": image_id,
                        "description": payload.description,
                        "source": "bot_sent_image",
                        "sent_by_bot": True,
                        "stored_in_database": True,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                created_at=created_at,
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def ensure_seeded(self) -> None:
        try:
            items = self.load_items()
        except Exception as exc:
            logger.warning("bot_image_catalog_load_failed path=%s error=%s", self.catalog_path, exc)
            return
        for item in items:
            path = self._resolve_path(item.path)
            if not path.exists() or not path.is_file():
                logger.warning("bot_image_catalog_missing_file image_id=%s path=%s", item.id, path)
                continue
            try:
                content = path.read_bytes()
            except OSError as exc:
                logger.warning("bot_image_catalog_read_failed image_id=%s error=%s", item.id, exc)
                continue
            if content:
                self._try_seed_item(item, content)

    def load_items(self) -> list[BotImageCatalogItem]:
        if not self.catalog_path.exists():
            return []
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8-sig"))
        entries = raw.get("images", raw if isinstance(raw, list) else [])
        items: list[BotImageCatalogItem] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            image_id = str(entry.get("id") or "").strip()
            path = str(entry.get("path") or "").strip()
            description = str(entry.get("description") or "").strip()
            if not image_id or not path or not description:
                continue
            mime_type = str(entry.get("mime_type") or mimetypes.guess_type(path)[0] or "image/jpeg").strip()
            tags = tuple(str(tag).strip() for tag in entry.get("tags", []) if str(tag).strip())
            items.append(BotImageCatalogItem(image_id, path, description, mime_type, tags))
        return items

    def _item_by_id(self, image_id: str) -> BotImageCatalogItem | None:
        image_id = (image_id or "").strip()
        for item in self.load_items():
            if item.id == image_id:
                return item
        return None

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        if path.parts and path.parts[0] == self.image_dir.name:
            return path
        return self.image_dir / path

    def _database_payload(self, item: BotImageCatalogItem) -> BotImagePayload | None:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MediaFileORM)
                .where(MediaFileORM.media_kind == "bot_image", MediaFileORM.file_bytes.is_not(None))
                .order_by(MediaFileORM.id.desc())
                .limit(100)
            ).all()
        for row in rows:
            metadata = self._loads_metadata(row.metadata_json)
            if metadata.get("catalog_id") != item.id or not row.file_bytes:
                continue
            filename = row.original_file_name or Path(item.path).name or f"{item.id}.jpg"
            return BotImagePayload(
                image_id=item.id,
                content=bytes(row.file_bytes),
                mime_type=row.mime_type or item.mime_type,
                filename=filename,
                description=item.description,
            )
        return None

    def _try_seed_item(self, item: BotImageCatalogItem, content: bytes) -> None:
        content_hash = hashlib.sha256(content).hexdigest()
        with suppress_database_errors("bot_image_catalog_seed_failed", item.id):
            with self.database.orm.session() as session:
                exists = session.scalar(
                    select(MediaFileORM.id)
                    .where(MediaFileORM.media_kind == "bot_image", MediaFileORM.content_hash == content_hash)
                    .limit(1)
                )
                if exists:
                    return
                session.add(
                    MediaFileORM(
                        user_id=0,
                        chat_id=None,
                        telegram_message_id=None,
                        telegram_file_id=f"local:{item.id}",
                        media_kind="bot_image",
                        mime_type=item.mime_type,
                        original_file_name=Path(item.path).name,
                        storage_path=str(self._resolve_path(item.path)),
                        content_hash=content_hash,
                        file_bytes=content,
                        file_size=len(content),
                        caption=item.description,
                        metadata_json=json.dumps(
                            {
                                "catalog_id": item.id,
                                "description": item.description,
                                "source": "local_image_catalog",
                                "stored_in_database": True,
                            },
                            ensure_ascii=False,
                        ),
                        created_at=datetime.now(UTC),
                    )
                )

    def _loads_metadata(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return value if isinstance(value, dict) else {"value": value}


class suppress_database_errors:
    def __init__(self, event: str, image_id: str) -> None:
        self.event = event
        self.image_id = image_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        if exc is None:
            return False
        logger.warning("%s image_id=%s error=%s", self.event, self.image_id, exc)
        return True


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
        image_bytes = media.file_bytes
        if image_bytes is None and media.storage_path:
            image_path = Path(media.storage_path)
            if image_path.exists() and image_path.stat().st_size > 0:
                image_bytes = image_path.read_bytes()
        if not image_bytes:
            raise VisionProviderError("stored image is missing")
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
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
