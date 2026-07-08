from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from bot.config import Settings
from bot.models.state import NargesSelfStateCandidate
from bot.services.global_state_service import GlobalStateService
from bot.services.history_service import HistoryService
from bot.services.billing_service import BillingService
from bot.services.group_service import HIDDEN_BOT_STATUSES
from bot.services.moderation_service import ModerationService
from bot.services.narges_state_service import NargesStateService
from bot.services.quota_service import QUOTA_UNIT_SCALE, QuotaService
from bot.services.required_channel_service import RequiredChannelService
from bot.storage.database import Database
from bot.storage.orm import (
    AdminBroadcastORM,
    AdminBroadcastDeliveryORM,
    AdminBypassORM,
    AiProviderKeyStatusORM,
    BillingInvoiceORM,
    ConversationContextStateORM,
    ConversationHistoryORM,
    ConversationMessageORM,
    ConversationSummaryORM,
    DailyEventORM,
    DebugLogORM,
    GroupChatORM,
    GroupEngineEventORM,
    MediaFileORM,
    MemoryORM,
    MemoryAuditLogORM,
    MembershipCacheORM,
    NargesSelfStateORM,
    NargesStateAuditLogORM,
    NargesStateSchedulerRunORM,
    QuotaEventORM,
    UsageLogORM,
    UserBlockORM,
    UserORM,
    UserWarningEventORM,
    RequiredChannelORM,
    ScheduledGroupMessageORM,
    Base,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def dt_iso(value: Any) -> str:
    if value is None:
        return "-"
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = utc_now() - value.astimezone(UTC)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "همین الان"
    if seconds < 3600:
        return f"{seconds // 60} دقیقه قبل"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت قبل"
    if seconds < 30 * 86400:
        return f"{seconds // 86400} روز قبل"
    if seconds < 365 * 86400:
        return f"{seconds // (30 * 86400)} ماه قبل"
    return f"{seconds // (365 * 86400)} سال قبل"


def sort_datetime(value: Any) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def mask_key(value: str) -> str:
    value = value.strip()
    if value.startswith("env:") or value.startswith("$"):
        return value
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:6]}...{value[-4:]}"


def compact_number(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000:
        return f"{sign}{number / 1_000_000:.1f}M".replace(".0M", "M")
    if number >= 1_000:
        return f"{sign}{number / 1_000:.1f}K".replace(".0K", "K")
    return f"{sign}{int(number)}"


@dataclass(frozen=True)
class DashboardSnapshot:
    users_total: int
    users_today: int
    messages_today: int
    warnings_today: int
    ai_errors_today: int
    active_blocks: int
    quota_events_today: int
    current_state: dict[str, Any]
    recent_debug: list[dict[str, Any]]
    recent_usage: list[dict[str, Any]]
    provider_statuses: list[dict[str, Any]]
    active_users_today: int
    tokens_today: int
    recent_messages: list[dict[str, Any]]
    recent_users: list[dict[str, Any]]
    daily_chart: list[dict[str, Any]]
    provider_token_chart: list[dict[str, Any]]
    hourly_usage_chart: list[dict[str, Any]]


class AdminDataService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.quota_service = QuotaService(database, settings)
        self.billing_service = BillingService(database)
        self.state_service = NargesStateService(database)
        self.global_state_service = GlobalStateService(database)
        self.moderation_service = ModerationService(database)
        self.channel_service = RequiredChannelService(database, settings.membership_cache_seconds, settings.admin_ids)

    def dashboard(self) -> DashboardSnapshot:
        today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        with self.database.orm.session() as session:
            users_total = session.scalar(select(func.count()).select_from(UserORM)) or 0
            users_today = session.scalar(select(func.count()).select_from(UserORM).where(UserORM.created_at >= today)) or 0
            messages_today = session.scalar(select(func.count()).select_from(ConversationMessageORM).where(ConversationMessageORM.created_at >= today)) or 0
            warnings_today = session.scalar(select(func.count()).select_from(UserWarningEventORM).where(UserWarningEventORM.created_at >= today)) or 0
            ai_errors_today = session.scalar(
                select(func.count())
                .select_from(DebugLogORM)
                .where(DebugLogORM.created_at >= today, DebugLogORM.event.like("%error%"))
            ) or 0
            active_blocks = session.scalar(select(func.count()).select_from(UserBlockORM).where(UserBlockORM.blocked_until > utc_now())) or 0
            quota_events_today = session.scalar(select(func.count()).select_from(QuotaEventORM).where(QuotaEventORM.created_at >= today)) or 0
            recent_debug_rows = session.scalars(select(DebugLogORM).order_by(desc(DebugLogORM.id)).limit(8)).all()
            recent_usage_rows = session.scalars(select(UsageLogORM).order_by(desc(UsageLogORM.id)).limit(8)).all()
            active_users_today = session.scalar(select(func.count(func.distinct(ConversationMessageORM.user_id))).where(ConversationMessageORM.created_at >= today)) or 0
            tokens_today = session.scalar(select(func.coalesce(func.sum(UsageLogORM.total_tokens), 0)).where(UsageLogORM.created_at >= today)) or 0
            recent_messages = session.scalars(select(ConversationMessageORM).order_by(desc(ConversationMessageORM.id)).limit(10)).all()
            recent_users = session.scalars(select(UserORM).order_by(desc(UserORM.created_at)).limit(10)).all()
            user_names = self._user_name_map(session, self._collect_user_ids(recent_messages, recent_usage_rows, recent_debug_rows))
            daily_chart = self._daily_chart(session, days=14)
            provider_token_chart = self._provider_token_chart(session, since=today - timedelta(days=13))
            hourly_usage_chart = self._hourly_usage_chart(session, since=today - timedelta(days=13), days=14)

        return DashboardSnapshot(
            users_total=int(users_total),
            users_today=int(users_today),
            messages_today=int(messages_today),
            warnings_today=int(warnings_today),
            ai_errors_today=int(ai_errors_today),
            active_blocks=int(active_blocks),
            quota_events_today=int(quota_events_today),
            current_state=self.state_service.get_active().model_dump(mode="json"),
            recent_debug=[self._debug_row(row, user_names) for row in recent_debug_rows],
            recent_usage=[self._usage_row(row, user_names) for row in recent_usage_rows],
            provider_statuses=self.provider_statuses(),
            active_users_today=int(active_users_today),
            tokens_today=int(tokens_today),
            recent_messages=[self._message_row(row, user_names) for row in recent_messages],
            recent_users=[self._user_row(row) for row in recent_users],
            daily_chart=daily_chart,
            provider_token_chart=provider_token_chart,
            hourly_usage_chart=hourly_usage_chart,
        )

    def users(self, sort: str = "last_seen", query: str = "", active_only: bool = False, ready_only: bool = False) -> list[dict[str, Any]]:
        with self.database.orm.session() as session:
            rows = session.scalars(select(UserORM)).all()
            last_seen_pairs = session.execute(
                select(ConversationMessageORM.user_id, func.max(ConversationMessageORM.created_at))
                .group_by(ConversationMessageORM.user_id)
            ).all()
            warning_pairs = session.execute(
                select(UserWarningEventORM.user_id, func.count()).group_by(UserWarningEventORM.user_id)
            ).all()
        last_seen = {int(user_id): value for user_id, value in last_seen_pairs}
        warnings = {int(user_id): int(value) for user_id, value in warning_pairs}
        items = []
        needle = query.strip().lower()
        for row in rows:
            haystack = " ".join(
                str(value or "")
                for value in [row.telegram_id, row.username, row.first_name, row.last_name, row.display_name, row.phone_number]
            ).lower()
            if needle and needle not in haystack:
                continue
            if active_only and row.telegram_id not in last_seen:
                continue
            if ready_only and row.onboarding_state != "ready":
                continue
            quota = self.quota_service.account_quota(row.telegram_id)
            items.append(
                {
                    "telegram_id": row.telegram_id,
                    "name": row.display_name or row.first_name or row.username or str(row.telegram_id),
                    "username": row.username,
                    "phone_number": row.phone_number,
                    "gender": row.gender,
                    "onboarding_state": row.onboarding_state,
                    "warnings": warnings.get(row.telegram_id, 0),
                    "daily_remaining": quota.daily_remaining // QUOTA_UNIT_SCALE,
                    "extra_remaining": quota.extra_remaining // QUOTA_UNIT_SCALE,
                    "last_seen": last_seen.get(row.telegram_id),
                    "updated_at": row.updated_at,
                    "created_at": row.created_at,
                }
            )
        sort_key = {
            "warnings": lambda item: item["warnings"],
            "quota": lambda item: item["daily_remaining"] + item["extra_remaining"],
            "created": lambda item: sort_datetime(item["created_at"]),
            "gender": lambda item: (0 if item["gender"] else 1, str(item["gender"] or "")),
        }.get(sort, lambda item: sort_datetime(item["last_seen"]))
        return sorted(items, key=sort_key, reverse=sort != "gender")

    def user_detail(self, user_id: int) -> dict[str, Any]:
        with self.database.orm.session() as session:
            user = session.get(UserORM, user_id)
            messages = session.scalars(
                select(ConversationMessageORM)
                .where(ConversationMessageORM.user_id == user_id)
                .order_by(desc(ConversationMessageORM.id))
                .limit(30)
            ).all()
            memories = session.scalars(
                select(MemoryORM)
                .where(MemoryORM.user_id == user_id)
                .order_by(MemoryORM.active.desc(), desc(MemoryORM.importance), desc(MemoryORM.updated_at))
                .limit(80)
            ).all()
            warnings = session.scalars(
                select(UserWarningEventORM).where(UserWarningEventORM.user_id == user_id).order_by(desc(UserWarningEventORM.id)).limit(20)
            ).all()
            block = session.get(UserBlockORM, user_id)
            usage = session.scalars(select(UsageLogORM).where(UsageLogORM.user_id == user_id).order_by(desc(UsageLogORM.id)).limit(30)).all()
            usage_totals = session.execute(
                select(
                    func.coalesce(func.sum(UsageLogORM.prompt_tokens), 0),
                    func.coalesce(func.sum(UsageLogORM.completion_tokens), 0),
                    func.coalesce(func.sum(UsageLogORM.total_tokens), 0),
                ).where(UsageLogORM.user_id == user_id)
            ).one()
            quota_events = session.scalars(select(QuotaEventORM).where(QuotaEventORM.user_id == user_id).order_by(desc(QuotaEventORM.id)).limit(40)).all()
            invoices = session.scalars(select(BillingInvoiceORM).where(BillingInvoiceORM.user_id == user_id).order_by(desc(BillingInvoiceORM.created_at)).limit(20)).all()
            memory_audits = session.scalars(select(MemoryAuditLogORM).where(MemoryAuditLogORM.user_id == user_id).order_by(desc(MemoryAuditLogORM.id)).limit(40)).all()
            media = session.scalars(select(MediaFileORM).where(MediaFileORM.user_id == user_id).order_by(desc(MediaFileORM.id)).limit(40)).all()
            media_by_message = self._media_by_message(session, messages)
            media_assistant_replies = self._assistant_replies_for_media(session, media)
        return {
            "user": user,
            "quota": self.quota_service.account_quota(user_id),
            "messages": list(reversed(messages)),
            "memories": memories,
            "warnings": warnings,
            "block": block,
            "usage": usage,
            "quota_events": quota_events,
            "invoices": invoices,
            "memory_audits": memory_audits,
            "media": [self._media_row(row, assistant_replies=media_assistant_replies) for row in media],
            "media_by_message": media_by_message,
            "usage_total_tokens": int(usage_totals[2] or 0),
            "usage_prompt_tokens": int(usage_totals[0] or 0),
            "usage_completion_tokens": int(usage_totals[1] or 0),
        }

    def message_detail(self, message_id: int) -> dict[str, Any]:
        with self.database.orm.session() as session:
            row = session.get(ConversationMessageORM, message_id)
            user_names = self._user_name_map(session, [row.user_id] if row else [])
            related_media = self._media_by_message(session, [row]).get(row.id, []) if row else []
        payload = {}
        if row and row.ai_request_payload_json:
            try:
                payload = json.loads(row.ai_request_payload_json)
            except json.JSONDecodeError:
                payload = {"raw": row.ai_request_payload_json}
        elif row:
            payload = {
                "source": "admin_fallback",
                "role": row.role,
                "message_type": row.message_type,
                "chat_id": row.chat_id,
                "telegram_message_id": row.telegram_message_id,
                "provider": row.provider,
                "model": row.model,
                "note": "This older row had no stored ai_request_payload; showing reconstructed audit metadata.",
            }
        return {"message": row, "ai_request": payload, "user_names": user_names, "related_media": related_media}

    def messages(self, user_id: int | None = None, limit: int = 300) -> dict[str, Any]:
        limit = max(20, min(limit, 1000))
        with self.database.orm.session() as session:
            statement = (
                select(ConversationMessageORM)
                .order_by(desc(ConversationMessageORM.id))
                .limit(limit)
            )
            if user_id is not None:
                statement = statement.where(ConversationMessageORM.user_id == user_id)
            else:
                statement = statement.where(~ConversationMessageORM.message_type.like("group_%"))
            rows = session.scalars(statement).all()
            user_names = self._user_name_map(session, [row.user_id for row in rows])
            media_by_message = self._media_by_message(session, rows)
        return {"messages": rows, "user_id": user_id, "limit": limit, "user_names": user_names, "media_by_message": media_by_message}

    def group_messages(self, chat_id: int | None = None, section: str = "all", limit: int = 300) -> dict[str, Any]:
        limit = max(20, min(limit, 1000))
        section = (section or "all").strip()
        message_types = {
            "mentions": ["group_mention"],
            "photos": ["group_photo"],
            "auto": ["group_auto"],
            "scheduled": ["group_scheduled", "group_admin"],
        }.get(section)
        with self.database.orm.session() as session:
            message_statement = (
                select(ConversationMessageORM)
                .where(
                    ConversationMessageORM.message_type.like("group_%"),
                    ConversationMessageORM.message_type != "group_observed",
                )
                .order_by(desc(ConversationMessageORM.id))
                .limit(limit)
            )
            event_statement = select(GroupEngineEventORM).order_by(desc(GroupEngineEventORM.id)).limit(200)
            if chat_id is not None:
                message_statement = message_statement.where(ConversationMessageORM.chat_id == chat_id)
                event_statement = event_statement.where(GroupEngineEventORM.chat_id == chat_id)
            if message_types:
                message_statement = message_statement.where(ConversationMessageORM.message_type.in_(message_types))
            groups = list(session.scalars(self._visible_groups_statement()).all())
            visible_chat_ids = [int(group.chat_id) for group in groups]
            if visible_chat_ids:
                message_statement = message_statement.where(ConversationMessageORM.chat_id.in_(visible_chat_ids))
                event_statement = event_statement.where(GroupEngineEventORM.chat_id.in_(visible_chat_ids))
            else:
                message_statement = message_statement.where(False)
                event_statement = event_statement.where(False)
            rows = session.scalars(message_statement).all()
            events = session.scalars(event_statement).all()
            group_names = self._group_name_map(groups)
            user_names = self._user_name_map(session, [row.user_id for row in rows] + [event.user_id for event in events if event.user_id])
            media_by_message = self._media_by_message(session, rows)
            counts = {
                "all": self._group_message_count(session, visible_chat_ids, None),
                "observed": 0,
                "mentions": self._group_message_count(session, visible_chat_ids, ("group_mention",)),
                "photos": self._group_message_count(session, visible_chat_ids, ("group_photo",)),
                "auto": self._group_message_count(session, visible_chat_ids, ("group_auto",)),
                "scheduled": self._group_message_count(session, visible_chat_ids, ("group_scheduled", "group_admin")),
            }
        return {
            "messages": rows,
            "events": [self._group_event_row(row, group_names, user_names) for row in events],
            "groups": groups,
            "group_names": group_names,
            "user_names": user_names,
            "media_by_message": media_by_message,
            "chat_id": chat_id,
            "section": section,
            "limit": limit,
            "counts": counts,
        }

    def record_admin_direct_message(self, user_id: int, text: str, telegram_message_id: int | None) -> None:
        HistoryService(self.database).add(
            user_id,
            "assistant",
            text,
            chat_id=user_id,
            telegram_message_id=telegram_message_id,
            message_type="admin_direct",
            ai_request_payload={
                "source": "admin_direct_message",
                "target_type": "user",
                "target_id": user_id,
                "telegram_message_id": telegram_message_id,
            },
        )

    def record_admin_group_message(self, chat_id: int, text: str, telegram_message_id: int | None) -> None:
        from bot.services.group_service import GroupService

        group_service = GroupService(self.database)
        group_service.record_outbound_message(
            chat_id=chat_id,
            text=text,
            message_type="group_admin",
            telegram_message_id=telegram_message_id,
            metadata={"source": "admin_group_message"},
        )
        group_service.record_engine_event(
            chat_id=chat_id,
            event_type="admin_group_message",
            telegram_message_id=telegram_message_id,
            metadata={"source": "admin_panel"},
        )

    def media(self, user_id: int | None = None, kind: str = "", q: str = "", limit: int = 240) -> dict[str, Any]:
        limit = max(20, min(limit, 1000))
        kind = (kind or "").strip()
        needle = (q or "").strip().lower()
        from bot.services.media_service import BotImageCatalog

        BotImageCatalog(self.settings, self.database).ensure_seeded()
        with self.database.orm.session() as session:
            statement = select(MediaFileORM).order_by(desc(MediaFileORM.id)).limit(limit)
            if user_id is not None:
                statement = statement.where(MediaFileORM.user_id == user_id)
            if kind and kind != "all":
                statement = statement.where(MediaFileORM.media_kind == kind)
            rows = session.scalars(statement).all()
            user_names = self._user_name_map(session, [row.user_id for row in rows])
            assistant_replies = self._assistant_replies_for_media(session, rows)
        items = [self._media_row(row, user_names, assistant_replies) for row in rows]
        if needle:
            items = [
                item
                for item in items
                if needle in " ".join(
                    str(value or "")
                    for value in (
                        item["user_name"],
                        item["user_id"],
                        item["media_kind"],
                        item["mime_type"],
                        item["caption"],
                        item["vision_description"],
                        item["original_file_name"],
                    )
                ).lower()
            ]
        if not kind or kind == "all":
            items = sorted(items, key=lambda item: (item["media_kind"] == "bot_image", -int(item["id"])))
        return {"media": items, "user_id": user_id, "kind": kind, "q": q, "limit": limit}

    def media_file(self, media_id: int) -> MediaFileORM | None:
        with self.database.orm.session() as session:
            row = session.get(MediaFileORM, media_id)
            if row is None:
                return None
            session.expunge(row)
            return row

    def media_file_payload(self, media_id: int) -> tuple[bytes | None, str | None]:
        from bot.services.media_service import MediaStorageService

        return MediaStorageService(self.settings, self.database).file_payload(media_id)

    def invoices(self, limit: int = 300) -> dict[str, Any]:
        with self.database.orm.session() as session:
            rows = session.scalars(select(BillingInvoiceORM).order_by(desc(BillingInvoiceORM.created_at)).limit(limit)).all()
            user_names = self._user_name_map(session, [row.user_id for row in rows])
        return {"invoices": rows, "plans": self.billing_service.plans, "user_names": user_names}

    def review_invoice(self, invoice_id: str, approve: bool, reviewer_id: int | None = None) -> tuple[bool, Any, str]:
        result = self.billing_service.review_card_invoice(invoice_id, approve=approve, reviewer_id=reviewer_id)
        if result.accepted and result.newly_paid and result.invoice:
            self.quota_service.add_extra_credit(result.invoice.user_id, result.invoice.message_quota, reason=f"card:{result.invoice.invoice_id}")
        return result.accepted, result.invoice, result.reason

    def export_backup(self) -> dict[str, Any]:
        exported: dict[str, Any] = {}
        with self.database.orm.session() as session:
            for table in Base.metadata.sorted_tables:
                rows = session.execute(select(table)).mappings().all()
                exported[table.name] = [
                    {key: self._json_value(value) for key, value in row.items()}
                    for row in rows
                ]
        return {
            "format": "narges-admin-backup-v1",
            "created_at": utc_now().isoformat(),
            "tables": exported,
        }

    def import_backup(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("tables"), dict):
            raise ValueError("backup JSON نامعتبر است.")
        report: dict[str, Any] = {"inserted": 0, "skipped": 0, "tables": {}}
        tables_payload = payload["tables"]
        with self.database.orm.session() as session:
            for table in Base.metadata.sorted_tables:
                raw_rows = tables_payload.get(table.name)
                if not isinstance(raw_rows, list):
                    continue
                table_report = {"inserted": 0, "skipped": 0}
                columns = {column.name: column for column in table.columns}
                pk_columns = [column for column in table.primary_key.columns]
                append_pk = (
                    len(pk_columns) == 1
                    and pk_columns[0].name == "id"
                    and isinstance(pk_columns[0].type, Integer)
                    and pk_columns[0].autoincrement in {True, "auto"}
                )
                for raw_row in raw_rows:
                    if not isinstance(raw_row, dict):
                        table_report["skipped"] += 1
                        continue
                    cleaned = {
                        key: self._db_value(columns[key], value)
                        for key, value in raw_row.items()
                        if key in columns and not (append_pk and key == pk_columns[0].name)
                    }
                    if not cleaned:
                        table_report["skipped"] += 1
                        continue
                    if not append_pk and pk_columns and self._primary_key_exists(session, table, pk_columns, cleaned):
                        table_report["skipped"] += 1
                        continue
                    try:
                        with session.begin_nested():
                            session.execute(table.insert().values(**cleaned))
                    except SQLAlchemyError:
                        table_report["skipped"] += 1
                        continue
                    table_report["inserted"] += 1
                report["tables"][table.name] = table_report
                report["inserted"] += table_report["inserted"]
                report["skipped"] += table_report["skipped"]
        return report

    def add_user_extra_credit(self, user_id: int, amount: int, reason: str) -> None:
        if amount > 0:
            self.quota_service.add_extra_credit(user_id, amount, reason=f"admin:{reason or 'manual'}")

    def add_user_warning(self, user_id: int, reason: str) -> None:
        reason = reason.strip() or "manual admin warning"
        self.moderation_service.apply_manual_warning(user_id, reason)

    def delete_user_warning(self, user_id: int, warning_id: int) -> bool:
        return self.moderation_service.delete_warning(user_id, warning_id)

    def add_memory_from_form(self, user_id: int, form: dict[str, Any]) -> None:
        from bot.models.ai import MemorySuggestion
        from bot.services.memory_service import MemoryService

        suggestion = MemorySuggestion(
            action="create",
            kind=str(form.get("kind", "preference")),
            summary=str(form.get("summary", "")).strip(),
            confidence=float(form.get("confidence", 1) or 1),
            importance=int(form.get("importance", 3) or 3),
        )
        MemoryService(self.database).apply_candidates(user_id, None, suggestion.summary, [suggestion], assistant_sourced=False)

    def delete_memory(self, user_id: int, memory_id: int) -> bool:
        from bot.services.memory_service import MemoryService

        return MemoryService(self.database).delete(user_id, memory_id)

    def delete_user_completely(self, user_id: int, confirm: str) -> bool:
        if confirm.strip() != str(user_id):
            return False
        with self.database.orm.session() as session:
            user = session.get(UserORM, user_id)
            if user is None:
                return False
            for model in (
                ConversationMessageORM,
                ConversationHistoryORM,
                MemoryORM,
                MemoryAuditLogORM,
                ConversationSummaryORM,
                ConversationContextStateORM,
                QuotaEventORM,
                UsageLogORM,
                BillingInvoiceORM,
                DebugLogORM,
                UserWarningEventORM,
                MembershipCacheORM,
                AdminBypassORM,
                MediaFileORM,
            ):
                session.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
            session.query(UserBlockORM).filter(UserBlockORM.user_id == user_id).delete(synchronize_session=False)
            session.query(UserORM).filter(UserORM.referred_by_user_id == user_id).update(
                {"referred_by_user_id": None},
                synchronize_session=False,
            )
            session.delete(user)
        return True

    def memories(self, user_id: int | None = None, limit: int = 200) -> dict[str, Any]:
        with self.database.orm.session() as session:
            memory_statement = select(MemoryORM).order_by(desc(MemoryORM.updated_at)).limit(limit)
            audit_statement = select(MemoryAuditLogORM).order_by(desc(MemoryAuditLogORM.id)).limit(limit)
            if user_id is not None:
                memory_statement = memory_statement.where(MemoryORM.user_id == user_id)
                audit_statement = audit_statement.where(MemoryAuditLogORM.user_id == user_id)
            memories = session.scalars(memory_statement).all()
            audits = session.scalars(audit_statement).all()
            user_names = self._user_name_map(session, [row.user_id for row in memories] + [row.user_id for row in audits])
        return {"memories": memories, "audits": audits, "user_id": user_id, "user_names": user_names}

    def model_state(self) -> dict[str, Any]:
        with self.database.orm.session() as session:
            states = session.scalars(select(NargesSelfStateORM).order_by(desc(NargesSelfStateORM.id)).limit(20)).all()
            audits = session.scalars(select(NargesStateAuditLogORM).order_by(desc(NargesStateAuditLogORM.id)).limit(30)).all()
            scheduler_runs = session.scalars(select(NargesStateSchedulerRunORM).order_by(desc(NargesStateSchedulerRunORM.created_at)).limit(30)).all()
            events = session.scalars(select(DailyEventORM).order_by(desc(DailyEventORM.start_at)).limit(60)).all()
        return {
            "active": self.state_service.get_active(),
            "global_state": self.global_state_service.get(),
            "states": states,
            "audits": audits,
            "scheduler_runs": scheduler_runs,
            "events": events,
        }

    def save_state_from_form(self, form: dict[str, Any]) -> tuple[bool, str]:
        try:
            candidate = NargesSelfStateCandidate(
                mood=str(form.get("mood", "")).strip(),
                energy=int(form.get("energy", 70)),
                activity=str(form.get("activity", "")).strip(),
                location=str(form.get("location", "")).strip(),
                is_alone=str(form.get("is_alone", "false")).lower() in {"1", "true", "on", "yes"},
                companions=self._split_lines(str(form.get("companions", ""))),
                mind_topics=self._split_lines(str(form.get("mind_topics", ""))),
                note=str(form.get("note", "")).strip() or None,
                confidence=1,
                reason="admin panel edit",
            )
        except Exception as exc:
            return False, f"داده نامعتبر است: {exc}"
        ok = self.state_service.save_candidate(candidate, source="admin_panel")
        return ok, "وضعیت ذخیره شد." if ok else "وضعیت توسط validator رد شد."

    def save_ai_toggle_from_form(self, form: dict[str, Any]) -> None:
        enabled = str(form.get("ai_enabled", "")).lower() in {"1", "true", "on", "yes"}
        self.global_state_service.set_ai_enabled(
            enabled,
            str(form.get("ai_disabled_message", "")).strip() or None,
        )

    def provider_config(self) -> list[dict[str, Any]]:
        providers = self._read_provider_config().get("providers", [])
        statuses = {(item["provider"], item["key_index"]): item for item in self.provider_statuses()}
        result = []
        for provider in providers:
            keys = list(provider.get("api_keys") or [])
            result.append(
                {
                    **provider,
                    "masked_keys": [mask_key(str(key)) for key in keys],
                    "key_statuses": [statuses.get((provider.get("name"), index), {}) for index, _key in enumerate(keys)],
                }
            )
        return result

    def provider_statuses(self) -> list[dict[str, Any]]:
        with self.database.orm.session() as session:
            rows = session.scalars(select(AiProviderKeyStatusORM).order_by(AiProviderKeyStatusORM.provider, AiProviderKeyStatusORM.key_index)).all()
        return [
            {
                "provider": row.provider,
                "key_index": row.key_index,
                "status": row.status,
                "error_count": row.error_count,
                "last_error": row.last_error,
                "disabled_until": row.disabled_until,
                "last_success_at": row.last_success_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    def add_provider_key(self, provider_name: str, key: str) -> None:
        data = self._read_provider_config()
        for provider in data.get("providers", []):
            if provider.get("name") == provider_name:
                provider.setdefault("api_keys", []).append(key.strip())
                self._write_provider_config(data)
                return

    def delete_provider_key(self, provider_name: str, key_index: int) -> None:
        data = self._read_provider_config()
        for provider in data.get("providers", []):
            if provider.get("name") == provider_name:
                keys = list(provider.get("api_keys") or [])
                if 0 <= key_index < len(keys):
                    keys.pop(key_index)
                    provider["api_keys"] = keys
                    self._write_provider_config(data)
                return

    def update_provider(self, provider_name: str, form: dict[str, Any]) -> None:
        data = self._read_provider_config()
        for provider in data.get("providers", []):
            if provider.get("name") == provider_name:
                provider["enabled"] = str(form.get("enabled", "")).lower() in {"1", "true", "on", "yes"}
                provider["model"] = str(form.get("model", provider.get("model", ""))).strip()
                provider["base_url"] = str(form.get("base_url", provider.get("base_url", ""))).strip()
                provider["response_format"] = str(form.get("response_format", provider.get("response_format", "json_object"))).strip()
                priority_raw = str(form.get("priority", provider.get("priority", ""))).strip()
                provider["priority"] = int(priority_raw) if priority_raw.lstrip("-").isdigit() else None
                provider["timeout_seconds"] = float(form.get("timeout_seconds", provider.get("timeout_seconds", 45)))
                provider["max_completion_tokens"] = int(form.get("max_completion_tokens", provider.get("max_completion_tokens", 512)))
                provider["use_proxy"] = str(form.get("use_proxy", "on")).lower() in {"1", "true", "on", "yes"}
                provider["health_check"] = str(form.get("health_check", "")).lower() in {"1", "true", "on", "yes"}
                provider["experimental"] = str(form.get("experimental", "")).lower() in {"1", "true", "on", "yes"}
                provider["prompt_cache"] = str(form.get("prompt_cache", "")).lower() in {"1", "true", "on", "yes"}
                self._write_provider_config(data)
                return

    def logs(self, kind: str = "debug", limit: int = 100) -> dict[str, Any]:
        limit = max(10, min(limit, 300))
        with self.database.orm.session() as session:
            debug_rows = session.scalars(select(DebugLogORM).order_by(desc(DebugLogORM.id)).limit(limit)).all()
            usage_rows = session.scalars(select(UsageLogORM).order_by(desc(UsageLogORM.id)).limit(limit)).all()
            warning_rows = session.scalars(select(UserWarningEventORM).order_by(desc(UserWarningEventORM.id)).limit(limit)).all()
            broadcasts = session.scalars(select(AdminBroadcastORM).order_by(desc(AdminBroadcastORM.id)).limit(30)).all()
            user_names = self._user_name_map(session, self._collect_user_ids(debug_rows, usage_rows, warning_rows))
        return {"kind": kind, "debug": debug_rows, "usage": usage_rows, "warnings": warning_rows, "broadcasts": broadcasts, "user_names": user_names}

    def create_broadcast(self, text: str, target_count: int, target_type: str = "users", target_value: str | None = None) -> int:
        with self.database.orm.session() as session:
            row = AdminBroadcastORM(
                text=text.strip(),
                target_count=target_count,
                target_type=target_type,
                target_value=target_value,
                status="running",
                created_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return int(row.id)

    def finish_broadcast(self, broadcast_id: int, sent: int, failed: int, error: str | None = None, deliveries: list[Any] | None = None) -> None:
        now = utc_now()
        with self.database.orm.session() as session:
            row = session.get(AdminBroadcastORM, broadcast_id)
            if not row:
                return
            row.sent_count = sent
            row.failed_count = failed
            row.status = "failed" if error else "done"
            row.error = error
            row.finished_at = now
            for item in deliveries or []:
                session.add(
                    AdminBroadcastDeliveryORM(
                        broadcast_id=broadcast_id,
                        target_id=int(item.target_id),
                        target_type=getattr(item, "target_type", None) or ("group" if row.target_type == "groups" else "user"),
                        telegram_message_id=item.telegram_message_id,
                        status=item.status,
                        error=item.error,
                        created_at=now,
                        updated_at=now,
                    )
                )

    def broadcast_detail(self, broadcast_id: int) -> dict[str, Any]:
        with self.database.orm.session() as session:
            broadcast = session.get(AdminBroadcastORM, broadcast_id)
            deliveries = session.scalars(
                select(AdminBroadcastDeliveryORM)
                .where(AdminBroadcastDeliveryORM.broadcast_id == broadcast_id)
                .order_by(desc(AdminBroadcastDeliveryORM.id))
                .limit(1000)
            ).all()
        return {"broadcast": broadcast, "deliveries": deliveries}

    def target_user_ids(self, only_ready: bool = True) -> list[int]:
        with self.database.orm.session() as session:
            statement = select(UserORM.telegram_id)
            if only_ready:
                statement = statement.where(UserORM.onboarding_state == "ready")
            return [int(value) for value in session.scalars(statement).all()]

    def target_group_ids(self) -> list[int]:
        with self.database.orm.session() as session:
            return [
                int(value)
                for value in session.scalars(
                    select(GroupChatORM.chat_id)
                    .where(
                        GroupChatORM.active.is_(True),
                        GroupChatORM.bot_status.not_in(HIDDEN_BOT_STATUSES) | GroupChatORM.bot_status.is_(None),
                    )
                    .order_by(GroupChatORM.chat_id)
                ).all()
            ]

    def group_chats(self) -> list[GroupChatORM]:
        with self.database.orm.session() as session:
            return list(session.scalars(self._visible_groups_statement()).all())

    def scheduled_group_messages(self) -> list[ScheduledGroupMessageORM]:
        with self.database.orm.session() as session:
            return list(session.scalars(select(ScheduledGroupMessageORM).order_by(desc(ScheduledGroupMessageORM.id))).all())

    def create_scheduled_group_message(self, text: str, interval_minutes: int, enabled: bool) -> None:
        from bot.services.group_service import GroupService

        GroupService(self.database).create_schedule(text, interval_minutes, enabled)

    def update_scheduled_group_message(self, schedule_id: int, text: str, interval_minutes: int, enabled: bool) -> bool:
        from bot.services.group_service import GroupService

        return GroupService(self.database).update_schedule(schedule_id, text, interval_minutes, enabled)

    def delete_scheduled_group_message(self, schedule_id: int) -> bool:
        from bot.services.group_service import GroupService

        return GroupService(self.database).delete_schedule(schedule_id)

    def channels(self) -> list[RequiredChannelORM]:
        with self.database.orm.session() as session:
            return list(session.scalars(select(RequiredChannelORM).order_by(RequiredChannelORM.position, RequiredChannelORM.id)).all())

    def save_channel_from_form(self, form: dict[str, Any]) -> None:
        channel_id = str(form.get("channel_id", "")).strip()
        title = str(form.get("title", "")).strip()
        if not channel_id or not title:
            return
        position_raw = str(form.get("position", "")).strip()
        position = int(position_raw) if position_raw.isdigit() else None
        self.channel_service.add_channel(
            admin_id=0,
            chat_id=channel_id,
            title=title,
            join_url=str(form.get("join_url", "")).strip() or None,
            is_private=str(form.get("is_private", "")).lower() in {"on", "true", "1", "yes"},
            position=position,
        )

    def update_channel_from_form(self, channel_id: int, form: dict[str, Any]) -> None:
        current = self.channel_service.get(channel_id)
        if current is None:
            return
        position_raw = str(form.get("position", current.position)).strip()
        self.channel_service.update_channel(
            admin_id=0,
            channel_id=channel_id,
            chat_id=str(form.get("chat_id", current.chat_id)).strip(),
            title=str(form.get("title", current.title)).strip(),
            join_url=str(form.get("join_url", current.join_url or "")).strip() or None,
            is_private=str(form.get("is_private", "")).lower() in {"on", "true", "1", "yes"},
            active=str(form.get("active", "")).lower() in {"on", "true", "1", "yes"},
            position=int(position_raw) if position_raw.isdigit() else current.position,
        )

    def _read_provider_config(self) -> dict[str, Any]:
        path = Path(self.settings.ai_providers_config)
        if not path.exists():
            return {"providers": []}
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def _write_provider_config(self, data: dict[str, Any]) -> None:
        path = Path(self.settings.ai_providers_config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _split_lines(self, value: str) -> list[str]:
        return [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]

    def _json_value(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    def _db_value(self, column, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(column.type, DateTime) and isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return value
        if isinstance(column.type, Boolean):
            return bool(value)
        return value

    def _primary_key_exists(self, session, table, pk_columns, cleaned: dict[str, Any]) -> bool:
        if any(column.name not in cleaned for column in pk_columns):
            return False
        statement = select(table).limit(1)
        for column in pk_columns:
            statement = statement.where(column == cleaned[column.name])
        return session.execute(statement).first() is not None

    def _collect_user_ids(self, *row_groups) -> list[int]:
        values: list[int] = []
        for rows in row_groups:
            for row in rows or []:
                user_id = getattr(row, "user_id", None)
                if user_id is not None:
                    values.append(int(user_id))
        return values

    def _user_name_map(self, session, user_ids: list[int]) -> dict[int, str]:
        ids = sorted({int(user_id) for user_id in user_ids if user_id is not None})
        if not ids:
            return {}
        rows = session.scalars(select(UserORM).where(UserORM.telegram_id.in_(ids))).all()
        return {row.telegram_id: self._display_user_name(row) for row in rows}

    def _display_user_name(self, row: UserORM | None) -> str:
        if row is None:
            return "-"
        base = row.display_name or row.first_name or row.username
        if base:
            return str(base)
        return f"کاربر {row.telegram_id}"

    def _group_name_map(self, groups: list[GroupChatORM]) -> dict[int, str]:
        return {
            int(group.chat_id): group.title or group.username or str(group.chat_id)
            for group in groups
        }

    def _visible_groups_statement(self):
        return (
            select(GroupChatORM)
            .where(
                GroupChatORM.active.is_(True),
                GroupChatORM.bot_status.not_in(HIDDEN_BOT_STATUSES) | GroupChatORM.bot_status.is_(None),
            )
            .order_by(desc(GroupChatORM.last_seen_at), GroupChatORM.chat_id)
        )

    def _group_message_count(self, session, chat_ids: list[int], message_types: tuple[str, ...] | None) -> int:
        if not chat_ids:
            return 0
        statement = (
            select(func.count())
            .select_from(ConversationMessageORM)
            .where(
                ConversationMessageORM.message_type.like("group_%"),
                ConversationMessageORM.message_type != "group_observed",
                ConversationMessageORM.chat_id.in_(chat_ids),
            )
        )
        if message_types:
            statement = statement.where(ConversationMessageORM.message_type.in_(message_types))
        return int(session.scalar(statement) or 0)

    def _group_event_row(
        self,
        row: GroupEngineEventORM,
        group_names: dict[int, str],
        user_names: dict[int, str],
    ) -> dict[str, Any]:
        metadata = self._loads_json(row.metadata_json)
        return {
            "id": row.id,
            "chat_id": row.chat_id,
            "group_name": group_names.get(int(row.chat_id), str(row.chat_id)),
            "user_id": row.user_id,
            "user_name": user_names.get(int(row.user_id), f"کاربر {row.user_id}") if row.user_id else "-",
            "event_type": row.event_type,
            "telegram_message_id": row.telegram_message_id,
            "metadata": metadata,
            "created_at": row.created_at,
        }

    def _media_by_message(self, session, messages: list[ConversationMessageORM | None]) -> dict[int, list[dict[str, Any]]]:
        pairs = [
            (row.id, row.chat_id, row.user_id, row.telegram_message_id)
            for row in messages
            if row is not None and row.telegram_message_id is not None
        ]
        if not pairs:
            return {}
        chat_ids = sorted({int(chat_id) for _message_id, chat_id, _user_id, _telegram_id in pairs if chat_id is not None})
        user_ids = sorted({int(user_id) for _message_id, _chat_id, user_id, _telegram_id in pairs})
        telegram_ids = sorted({int(telegram_id) for _message_id, _chat_id, _user_id, telegram_id in pairs})
        rows = session.scalars(
            select(MediaFileORM).where(
                MediaFileORM.user_id.in_(user_ids),
                MediaFileORM.telegram_message_id.in_(telegram_ids),
                MediaFileORM.chat_id.in_(chat_ids) if chat_ids else True,
            )
        ).all()
        assistant_replies = self._assistant_replies_for_media(session, rows)
        bucket: dict[tuple[int | None, int, int], list[dict[str, Any]]] = {}
        for row in rows:
            bucket.setdefault((row.chat_id, int(row.user_id), int(row.telegram_message_id or 0)), []).append(self._media_row(row, assistant_replies=assistant_replies))
        return {
            int(message_id): bucket.get((chat_id, int(user_id), int(telegram_id or 0)), [])
            for message_id, chat_id, user_id, telegram_id in pairs
        }

    def _assistant_replies_for_media(self, session, media_rows: list[MediaFileORM]) -> dict[int, dict[str, Any]]:
        keys = [
            (row.id, row.chat_id, row.user_id, row.telegram_message_id)
            for row in media_rows
            if row.telegram_message_id is not None
        ]
        if not keys:
            return {}
        user_ids = sorted({int(user_id) for _media_id, _chat_id, user_id, _telegram_id in keys})
        telegram_ids = sorted({int(telegram_id) for _media_id, _chat_id, _user_id, telegram_id in keys})
        chat_ids = sorted({int(chat_id) for _media_id, chat_id, _user_id, _telegram_id in keys if chat_id is not None})
        user_messages = session.scalars(
            select(ConversationMessageORM).where(
                ConversationMessageORM.role == "user",
                ConversationMessageORM.user_id.in_(user_ids),
                ConversationMessageORM.telegram_message_id.in_(telegram_ids),
                ConversationMessageORM.chat_id.in_(chat_ids) if chat_ids else True,
            )
        ).all()
        user_by_key = {
            (row.chat_id, int(row.user_id), int(row.telegram_message_id or 0)): row
            for row in user_messages
        }
        if not user_by_key:
            return {}
        assistant_rows = session.scalars(
            select(ConversationMessageORM)
            .where(
                ConversationMessageORM.role == "assistant",
                ConversationMessageORM.user_id.in_(user_ids),
                ConversationMessageORM.chat_id.in_(chat_ids) if chat_ids else True,
            )
            .order_by(ConversationMessageORM.id.asc())
            .limit(3000)
        ).all()
        replies: dict[int, dict[str, Any]] = {}
        for media_id, chat_id, user_id, telegram_id in keys:
            user_message = user_by_key.get((chat_id, int(user_id), int(telegram_id or 0)))
            if user_message is None:
                continue
            assistant = next(
                (
                    row
                    for row in assistant_rows
                    if row.chat_id == chat_id
                    and int(row.user_id) == int(user_id)
                    and int(row.id) > int(user_message.id)
                ),
                None,
            )
            if assistant is not None:
                replies[int(media_id)] = {
                    "id": assistant.id,
                    "text": assistant.text,
                    "provider": assistant.provider,
                    "model": assistant.model,
                    "created_at": assistant.created_at,
                }
        return replies

    def _media_row(
        self,
        row: MediaFileORM,
        user_names: dict[int, str] | None = None,
        assistant_replies: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        metadata = self._loads_json(row.metadata_json)
        user_name = (user_names or {}).get(row.user_id, f"کاربر {row.user_id}")
        if row.media_kind == "bot_image" and int(row.user_id) == 0:
            user_name = "گالری نرگس"
        return {
            "id": row.id,
            "user_id": row.user_id,
            "user_name": user_name,
            "chat_id": row.chat_id,
            "telegram_message_id": row.telegram_message_id,
            "telegram_file_id": row.telegram_file_id,
            "media_kind": row.media_kind,
            "mime_type": row.mime_type,
            "original_file_name": row.original_file_name,
            "storage_path": row.storage_path,
            "content_hash": row.content_hash,
            "stored_in_database": bool(row.file_bytes),
            "file_size": row.file_size,
            "caption": row.caption,
            "metadata": metadata,
            "vision_description": metadata.get("vision_description") or metadata.get("description") or "",
            "assistant_reply": (assistant_replies or {}).get(int(row.id)),
            "created_at": row.created_at,
        }

    def _loads_json(self, raw: str | None) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return value if isinstance(value, dict) else {"value": value}

    def _debug_row(self, row: DebugLogORM, user_names: dict[int, str] | None = None) -> dict[str, Any]:
        return {
            "id": row.id,
            "event": row.event,
            "user_id": row.user_id,
            "user_name": (user_names or {}).get(row.user_id, f"کاربر {row.user_id}") if row.user_id else "-",
            "payload": row.payload,
            "created_at": row.created_at,
        }

    def _message_row(self, row: ConversationMessageORM, user_names: dict[int, str] | None = None) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "user_name": (user_names or {}).get(row.user_id, f"کاربر {row.user_id}"),
            "role": row.role,
            "text": row.text,
            "provider": row.provider,
            "model": row.model,
            "total_tokens": row.total_tokens,
            "created_at": row.created_at,
        }

    def _user_row(self, row: UserORM) -> dict[str, Any]:
        return {
            "telegram_id": row.telegram_id,
            "name": row.display_name or row.first_name or row.username or row.telegram_id,
            "username": row.username,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _usage_row(self, row: UsageLogORM, user_names: dict[int, str] | None = None) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "user_name": (user_names or {}).get(row.user_id, f"کاربر {row.user_id}") if row.user_id else "-",
            "provider": row.provider,
            "model": row.model,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "created_at": row.created_at,
        }

    def _daily_chart(self, session, days: int = 14) -> list[dict[str, Any]]:
        start_date = utc_now().date() - timedelta(days=days - 1)
        buckets = {
            (start_date + timedelta(days=offset)).isoformat(): {"date": (start_date + timedelta(days=offset)).isoformat(), "users": 0, "messages": 0, "tokens": 0}
            for offset in range(days)
        }
        start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
        user_rows = session.execute(
            select(UserORM.created_at).where(UserORM.created_at >= start_dt)
        ).all()
        message_rows = session.execute(
            select(ConversationMessageORM.created_at).where(ConversationMessageORM.created_at >= start_dt)
        ).all()
        usage_rows = session.execute(
            select(UsageLogORM.created_at, UsageLogORM.total_tokens).where(UsageLogORM.created_at >= start_dt)
        ).all()
        for (created_at,) in user_rows:
            key = self._date_key(created_at)
            if key in buckets:
                buckets[key]["users"] += 1
        for (created_at,) in message_rows:
            key = self._date_key(created_at)
            if key in buckets:
                buckets[key]["messages"] += 1
        for created_at, total_tokens in usage_rows:
            key = self._date_key(created_at)
            if key in buckets:
                buckets[key]["tokens"] += int(total_tokens or 0)
        return list(buckets.values())

    def _provider_token_chart(self, session, since: datetime) -> list[dict[str, Any]]:
        rows = session.execute(
            select(UsageLogORM.provider, func.coalesce(func.sum(UsageLogORM.total_tokens), 0))
            .where(UsageLogORM.created_at >= since)
            .group_by(UsageLogORM.provider)
            .order_by(desc(func.coalesce(func.sum(UsageLogORM.total_tokens), 0)))
        ).all()
        return [{"provider": provider or "unknown", "tokens": int(tokens or 0)} for provider, tokens in rows]

    def _hourly_usage_chart(self, session, since: datetime, days: int) -> list[dict[str, Any]]:
        rows = session.execute(
            select(UsageLogORM.created_at, UsageLogORM.total_tokens)
            .where(UsageLogORM.created_at >= since)
        ).all()
        buckets = {hour: {"hour": hour, "requests": 0, "tokens": 0} for hour in range(24)}
        for created_at, total_tokens in rows:
            parsed = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(str(created_at))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            hour = parsed.astimezone(UTC).hour
            buckets[hour]["requests"] += 1
            buckets[hour]["tokens"] += int(total_tokens or 0)
        divisor = max(1, days)
        return [
            {
                "hour": f"{hour:02d}:00",
                "avg_requests": round(bucket["requests"] / divisor, 2),
                "avg_tokens": round(bucket["tokens"] / divisor, 2),
            }
            for hour, bucket in buckets.items()
        ]

    def _date_key(self, value: datetime | str) -> str:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).date().isoformat()
