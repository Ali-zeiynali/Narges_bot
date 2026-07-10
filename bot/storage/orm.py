from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool


class Base(DeclarativeBase):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserORM(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(128))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    display_name: Mapped[str | None] = mapped_column(String(128))
    gender: Mapped[str | None] = mapped_column(String(32))
    suggested_name: Mapped[str | None] = mapped_column(String(128))
    pending_name: Mapped[str | None] = mapped_column(String(128))
    onboarding_state: Mapped[str] = mapped_column(String(64), default="new")
    registration_state: Mapped[str] = mapped_column(String(64), default="new")
    membership_state: Mapped[str] = mapped_column(String(64), default="unknown")
    last_membership_gate_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    last_membership_gate_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_prompt_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    last_prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_gender_nudge_date: Mapped[str | None] = mapped_column(String(16))
    last_reengagement_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_code: Mapped[str | None] = mapped_column(String(64), unique=True)
    referred_by_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    first_question_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_bonus_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    name_confirm_attempted: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    phone_number: Mapped[str | None] = mapped_column(String(32))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_bonus_claimed: Mapped[bool] = mapped_column(Boolean, default=False)
    biography: Mapped[str | None] = mapped_column(Text)
    profile_completion_state: Mapped[str] = mapped_column(String(32), default="idle")
    profile_invalid_attempts: Mapped[int] = mapped_column(Integer, default=0)
    quota_profile_prompt_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MemoryORM(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    importance: Mapped[int] = mapped_column(Integer, default=3)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_memories_user_active_orm", "user_id", "active", "kind"),)


class MemoryAuditLogORM(Base):
    __tablename__ = "memory_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    memory_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    before_payload: Mapped[str | None] = mapped_column(Text)
    after_payload: Mapped[str | None] = mapped_column(Text)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationHistoryORM(Base):
    __tablename__ = "conversation_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(32))
    text_hash: Mapped[str] = mapped_column(String(128))
    text_preview: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationMessageORM(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(String(32))
    message_type: Mapped[str] = mapped_column(String(32), default="chat")
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    provider_response_id: Mapped[str | None] = mapped_column(String(128))
    safety_metadata_json: Mapped[str | None] = mapped_column("safety_metadata", Text)
    tone_metadata_json: Mapped[str | None] = mapped_column("tone_metadata", Text)
    ai_request_payload_json: Mapped[str | None] = mapped_column("ai_request_payload", Text)
    intent: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationSummaryORM(Base):
    __tablename__ = "conversation_summaries"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    summarized_message_id: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationContextStateORM(Base):
    __tablename__ = "conversation_context_states"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(64), default="casual")
    topic: Mapped[str | None] = mapped_column(Text)
    recent_intent: Mapped[str] = mapped_column(String(64), default="casual")
    intent_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    relationship_stage: Mapped[str] = mapped_column(String(64), default="new")
    trust_level: Mapped[float] = mapped_column(Float, default=0.0)
    familiarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    last_user_message_hash: Mapped[str | None] = mapped_column(String(128))
    last_assistant_text_hash: Mapped[str | None] = mapped_column(String(128))
    last_assistant_intent: Mapped[str | None] = mapped_column(String(64))
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QuotaEventORM(Base):
    __tablename__ = "quota_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    cost: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UsageLogORM(Base):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(64), default="groq")
    model: Mapped[str] = mapped_column(String(128))
    estimated_tokens: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(64), default="chat_reply")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MediaFileORM(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_file_id: Mapped[str] = mapped_column(String(256))
    media_kind: Mapped[str] = mapped_column(String(32), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    original_file_name: Mapped[str | None] = mapped_column(String(256))
    storage_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    file_size: Mapped[int | None] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("idx_media_files_user_kind_created", "user_id", "media_kind", "created_at"),
        Index("idx_media_files_content_hash", "content_hash"),
    )


class AiProviderKeyStatusORM(Base):
    __tablename__ = "ai_provider_key_statuses"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    disabled_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AdminBroadcastORM(Base):
    __tablename__ = "admin_broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text)
    target_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="created")
    error: Mapped[str | None] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(String(32), default="users")
    target_value: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminBroadcastDeliveryORM(Base):
    __tablename__ = "admin_broadcast_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broadcast_id: Mapped[int] = mapped_column(Integer, index=True)
    target_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="user")
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="created")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupChatORM(Base):
    __tablename__ = "group_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(256))
    username: Mapped[str | None] = mapped_column(String(128))
    chat_type: Mapped[str] = mapped_column(String(32))
    bot_status: Mapped[str | None] = mapped_column(String(64))
    member_count: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GroupInviteRewardORM(Base):
    __tablename__ = "group_invite_rewards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    member_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_status: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("idx_group_invite_rewards_user_chat", "user_id", "chat_id", unique=True),)


class ScheduledGroupMessageORM(Base):
    __tablename__ = "scheduled_group_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupEngineEventORM(Base):
    __tablename__ = "group_engine_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("idx_group_engine_events_chat_type_created", "chat_id", "event_type", "created_at"),
        Index("idx_group_engine_events_user_type_created", "user_id", "event_type", "created_at"),
    )


class UserWarningEventORM(Base):
    __tablename__ = "user_warning_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    warning_count_after: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UserBlockORM(Base):
    __tablename__ = "user_blocks"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    warning_count: Mapped[int] = mapped_column(Integer)
    blocked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DebugLogORM(Base):
    __tablename__ = "debug_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BillingInvoiceORM(Base):
    __tablename__ = "billing_invoices"

    invoice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plan_id: Mapped[str] = mapped_column(String(64), index=True)
    stars_cost: Mapped[int] = mapped_column(Integer)
    message_quota: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    payment_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GlobalStateORM(Base):
    __tablename__ = "global_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DailyEventORM(Base):
    __tablename__ = "daily_events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_date: Mapped[str] = mapped_column(String(16), index=True)
    payload: Mapped[str] = mapped_column(Text)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RequiredChannelORM(Base):
    __tablename__ = "required_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(256), unique=True)
    title: Mapped[str] = mapped_column(String(256))
    join_url: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=100)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChannelAuditLogORM(Base):
    __tablename__ = "channel_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[int | None] = mapped_column(Integer)
    before_payload: Mapped[str | None] = mapped_column(Text)
    after_payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MembershipCacheORM(Base):
    __tablename__ = "membership_cache"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str | None] = mapped_column(String(64))
    is_member: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdminBypassORM(Base):
    __tablename__ = "admin_bypasses"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bypass_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NargesSelfStateORM(Base):
    __tablename__ = "narges_self_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payload: Mapped[str] = mapped_column(Text)
    mood: Mapped[str] = mapped_column(String(64))
    energy: Mapped[int] = mapped_column(Integer)
    activity: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text)
    is_alone: Mapped[bool] = mapped_column(Boolean)
    companions: Mapped[str | None] = mapped_column(Text)
    mind_topics: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NargesStateAuditLogORM(Base):
    __tablename__ = "narges_state_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    before_payload: Mapped[str | None] = mapped_column(Text)
    after_payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NargesStateSchedulerRunORM(Base):
    __tablename__ = "narges_state_scheduler_runs"

    run_date: Mapped[str] = mapped_column(String(16), primary_key=True)
    slot: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DatabaseSessionManager:
    def __init__(self, database_url: str) -> None:
        url = normalize_database_url(database_url)
        parsed = make_url(url)
        if parsed.drivername.startswith("sqlite") and parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        engine_kwargs = {"future": True, "pool_pre_ping": True}
        if parsed.drivername.startswith("sqlite") and parsed.database == ":memory:":
            engine_kwargs.update({"poolclass": StaticPool, "connect_args": {"check_same_thread": False}})
        elif parsed.drivername.startswith("sqlite"):
            engine_kwargs.update({"poolclass": NullPool})
        self.engine = create_engine(url, **engine_kwargs)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False, future=True)

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name

    @property
    def is_sqlite(self) -> bool:
        return self.dialect_name == "sqlite"

    def ensure_schema(self) -> None:
        for table in Base.metadata.sorted_tables:
            table.create(bind=self.engine, checkfirst=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def normalize_database_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        value = "data/narges.sqlite3"
    if value == ":memory:":
        return "sqlite:///:memory:"
    if "://" not in value:
        return f"sqlite:///{Path(value).as_posix()}"
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value.removeprefix("postgresql://")
    if "supabase.com" in value and "sslmode=" not in value:
        separator = "&" if "?" in value else "?"
        value = f"{value}{separator}sslmode=require"
    return value
