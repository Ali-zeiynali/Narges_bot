from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select

from bot.config import Settings
from bot.models.state import NargesSelfStateCandidate
from bot.services.global_state_service import GlobalStateService
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


class AdminDataService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.quota_service = QuotaService(database, settings)
        self.state_service = NargesStateService(database)
        self.global_state_service = GlobalStateService(database)
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

        return DashboardSnapshot(
            users_total=int(users_total),
            users_today=int(users_today),
            messages_today=int(messages_today),
            warnings_today=int(warnings_today),
            ai_errors_today=int(ai_errors_today),
            active_blocks=int(active_blocks),
            quota_events_today=int(quota_events_today),
            current_state=self.state_service.get_active().model_dump(mode="json"),
            recent_debug=[self._debug_row(row) for row in recent_debug_rows],
            recent_usage=[self._usage_row(row) for row in recent_usage_rows],
            provider_statuses=self.provider_statuses(),
            active_users_today=int(active_users_today),
            tokens_today=int(tokens_today),
            recent_messages=[self._message_row(row) for row in recent_messages],
            recent_users=[self._user_row(row) for row in recent_users],
        )

    def users(self, sort: str = "last_seen", query: str = "") -> list[dict[str, Any]]:
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
            quota = self.quota_service.account_quota(row.telegram_id)
            items.append(
                {
                    "telegram_id": row.telegram_id,
                    "name": row.display_name or row.first_name or row.username or str(row.telegram_id),
                    "username": row.username,
                    "phone_number": row.phone_number,
                    "plan": row.plan,
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
            "created": lambda item: item["created_at"] or datetime.min,
        }.get(sort, lambda item: item["last_seen"] or datetime.min)
        return sorted(items, key=sort_key, reverse=True)

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
            "usage_total_tokens": int(usage_totals[2] or 0),
            "usage_prompt_tokens": int(usage_totals[0] or 0),
            "usage_completion_tokens": int(usage_totals[1] or 0),
        }

    def message_detail(self, message_id: int) -> dict[str, Any]:
        with self.database.orm.session() as session:
            row = session.get(ConversationMessageORM, message_id)
        payload = {}
        if row and row.ai_request_payload_json:
            try:
                payload = json.loads(row.ai_request_payload_json)
            except json.JSONDecodeError:
                payload = {"raw": row.ai_request_payload_json}
        return {"message": row, "ai_request": payload}

    def messages(self, user_id: int | None = None, limit: int = 300) -> dict[str, Any]:
        limit = max(20, min(limit, 1000))
        with self.database.orm.session() as session:
            statement = select(ConversationMessageORM).order_by(desc(ConversationMessageORM.id)).limit(limit)
            if user_id is not None:
                statement = statement.where(ConversationMessageORM.user_id == user_id)
            rows = session.scalars(statement).all()
        return {"messages": rows, "user_id": user_id, "limit": limit}

    def invoices(self, limit: int = 300) -> dict[str, Any]:
        with self.database.orm.session() as session:
            rows = session.scalars(select(BillingInvoiceORM).order_by(desc(BillingInvoiceORM.created_at)).limit(limit)).all()
        return {"invoices": rows}

    def add_user_extra_credit(self, user_id: int, amount: int, reason: str) -> None:
        if amount > 0:
            self.quota_service.add_extra_credit(user_id, amount, reason=f"admin:{reason or 'manual'}")

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
        return {"memories": memories, "audits": audits, "user_id": user_id}

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
        return {"kind": kind, "debug": debug_rows, "usage": usage_rows, "warnings": warning_rows, "broadcasts": broadcasts}

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
                        target_type="group" if row.target_type == "groups" else "user",
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
                    select(GroupChatORM.chat_id).where(GroupChatORM.active.is_(True)).order_by(GroupChatORM.chat_id)
                ).all()
            ]

    def group_chats(self) -> list[GroupChatORM]:
        with self.database.orm.session() as session:
            return list(session.scalars(select(GroupChatORM).order_by(desc(GroupChatORM.last_seen_at), GroupChatORM.chat_id)).all())

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

    def _debug_row(self, row: DebugLogORM) -> dict[str, Any]:
        return {"id": row.id, "event": row.event, "user_id": row.user_id, "payload": row.payload, "created_at": row.created_at}

    def _message_row(self, row: ConversationMessageORM) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "role": row.role,
            "text": row.text[:180],
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

    def _usage_row(self, row: UsageLogORM) -> dict[str, Any]:
        return {
            "id": row.id,
            "user_id": row.user_id,
            "provider": row.provider,
            "model": row.model,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "created_at": row.created_at,
        }
