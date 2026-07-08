from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot

from bot.services.media_service import MediaStorageError, MediaStorageService, VisionClient, VisionProviderError
from bot.services.user_service import UserService


logger = logging.getLogger(__name__)


class ProfilePhotoService:
    def __init__(
        self,
        *,
        media_storage_service: MediaStorageService,
        vision_client: VisionClient,
        user_service: UserService,
        sync_interval_hours: int = 24,
    ) -> None:
        self.media_storage_service = media_storage_service
        self.vision_client = vision_client
        self.user_service = user_service
        self.sync_interval_hours = sync_interval_hours
        self._inflight: set[int] = set()

    def context_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return self.media_storage_service.profile_photo_context(user_id, limit=3)

    def schedule_sync(self, bot: Bot, user_id: int, *, force: bool = False) -> None:
        if user_id in self._inflight:
            return
        if not force and self.media_storage_service.profile_photos_recently_synced(user_id, self.sync_interval_hours):
            return
        self._inflight.add(user_id)
        asyncio.create_task(self._sync_user_guarded(bot, user_id, force=force), name=f"profile-photo-sync-{user_id}")

    async def sync_known_users(self, bot: Bot, *, pause_seconds: float = 0.35) -> None:
        for user_id in self.user_service.user_ids():
            try:
                await self.sync_user(bot, user_id)
            except Exception:
                logger.exception("profile_photo_known_user_sync_failed user_id=%s", user_id)
            await asyncio.sleep(pause_seconds)

    async def run_known_user_sync_forever(self, bot: Bot) -> None:
        while True:
            await self.sync_known_users(bot)
            await asyncio.sleep(self.sync_interval_hours * 60 * 60)

    async def sync_user(self, bot: Bot, user_id: int, *, force: bool = False) -> None:
        if not force and self.media_storage_service.profile_photos_recently_synced(user_id, self.sync_interval_hours):
            return
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=3)
        photo_groups = list(photos.photos or [])[:3]
        self.media_storage_service.mark_profile_photos_synced(user_id, int(getattr(photos, "total_count", 0) or len(photo_groups)))
        for position, sizes in enumerate(photo_groups):
            photo = self._largest_photo(sizes)
            if photo is None:
                continue
            try:
                stored = await self.media_storage_service.store_profile_photo(bot, user_id, photo, position)
            except MediaStorageError as exc:
                logger.info("profile_photo_store_skipped user_id=%s position=%s error=%s", user_id, position, exc)
                continue
            description = self.media_storage_service.cached_vision_description(stored)
            if not description:
                try:
                    description = await asyncio.to_thread(self.vision_client.describe_image, stored)
                except VisionProviderError as exc:
                    logger.warning("profile_photo_vision_failed user_id=%s media_id=%s error=%s", user_id, stored.id, exc)
                    continue
            self.media_storage_service.set_vision_description(stored.id, description)

    async def _sync_user_guarded(self, bot: Bot, user_id: int, *, force: bool) -> None:
        try:
            await self.sync_user(bot, user_id, force=force)
        except Exception:
            logger.exception("profile_photo_sync_failed user_id=%s", user_id)
        finally:
            self._inflight.discard(user_id)

    def _largest_photo(self, sizes: list[Any] | tuple[Any, ...]) -> Any | None:
        if not sizes:
            return None
        return max(sizes, key=lambda item: getattr(item, "file_size", None) or (getattr(item, "width", 0) * getattr(item, "height", 0)))
