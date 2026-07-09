import json
import re
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from bot.models.state import NargesSelfState, NargesSelfStateCandidate
from bot.storage.database import Database
from bot.storage.orm import NargesSelfStateORM, NargesStateAuditLogORM, NargesStateSchedulerRunORM


BLOCKED_STATE_WORDS = [
    "system prompt",
    "developer message",
    "ignore previous",
    "api key",
    "token",
    "password",
    "پرامپت سیستم",
    "دستور قبلی",
]


class NargesStateService:
    def __init__(self, database: Database, cache_ttl_seconds: int = 15) -> None:
        self.database = database
        self.cache_ttl = timedelta(seconds=max(1, int(cache_ttl_seconds)))
        self._cache_lock = threading.RLock()
        self._cached_state: NargesSelfState | None = None
        self._cache_expires_at: datetime | None = None

    def get_active(self) -> NargesSelfState:
        cached = self._get_cached()
        if cached is not None:
            return cached
        with self.database.orm.session() as session:
            row = session.scalar(
                select(NargesSelfStateORM)
                .where(NargesSelfStateORM.is_active.is_(True))
                .order_by(NargesSelfStateORM.id.desc())
                .limit(1)
            )
        if row is None:
            state = NargesSelfState(updated_at=datetime.now(UTC))
            self.save_candidate(
                NargesSelfStateCandidate(
                    mood=state.mood,
                    energy=state.energy,
                    activity=state.activity,
                    location=state.location,
                    is_alone=state.is_alone,
                    companions=state.companions,
                    mind_topics=state.mind_topics,
                    note=state.note,
                    confidence=1,
                    reason="initial default state",
                ),
                source="default",
            )
            self._set_cached(state)
            return state
        state = NargesSelfState.model_validate(json.loads(row.payload))
        self._set_cached(state)
        return state

    def save_candidate(self, candidate: NargesSelfStateCandidate, source: str) -> bool:
        before = self._active_payload()
        ok, reason = self.validate_candidate(candidate)
        if not ok:
            self._audit("state_update", "rejected", reason, before, candidate.model_dump())
            return False

        now = datetime.now(UTC)
        state = NargesSelfState(
            mood=candidate.mood.strip(),
            energy=candidate.energy,
            activity=candidate.activity.strip(),
            location=candidate.location.strip(),
            is_alone=candidate.is_alone,
            companions=candidate.companions,
            mind_topics=candidate.mind_topics,
            note=candidate.note,
            updated_at=now,
        )
        with self.database.orm.session() as session:
            active_rows = session.scalars(select(NargesSelfStateORM).where(NargesSelfStateORM.is_active.is_(True))).all()
            for row in active_rows:
                row.is_active = False
            session.flush()
            session.add(
                NargesSelfStateORM(
                    payload=state.model_dump_json(),
                    mood=state.mood,
                    energy=state.energy,
                    activity=state.activity,
                    location=state.location,
                    is_alone=state.is_alone,
                    companions=json.dumps(state.companions, ensure_ascii=False),
                    mind_topics=json.dumps(state.mind_topics, ensure_ascii=False),
                    source=source,
                    is_active=True,
                    created_at=now,
                )
            )
        self._set_cached(state)
        self._audit("state_update", "accepted", candidate.reason, before, state.model_dump(mode="json"))
        return True

    def validate_candidate(self, candidate: NargesSelfStateCandidate) -> tuple[bool, str]:
        text = " ".join(
            [
                candidate.mood,
                candidate.activity,
                candidate.location,
                candidate.note or "",
                " ".join(candidate.companions),
                " ".join(candidate.mind_topics),
            ]
        ).lower()
        if candidate.confidence < 0.45:
            return False, "low confidence"
        if any(word in text for word in BLOCKED_STATE_WORDS):
            return False, "unsafe state content"
        if re.search(r"https?://|www\.|@[\w_]{3,}", text):
            return False, "state contains url or username"
        if candidate.energy < 15 and any(word in candidate.activity.lower() for word in ["running", "party", "ورزش سنگین"]):
            return False, "energy and activity conflict"
        if candidate.is_alone and candidate.companions:
            return False, "alone with companions conflict"
        if len(set(topic.lower() for topic in candidate.mind_topics)) != len(candidate.mind_topics):
            return False, "duplicate mind topics"
        return True, "accepted"

    def mark_scheduler_run(self, run_date: str, slot: str, status: str, error: str | None = None) -> None:
        with self.database.orm.session() as session:
            row = session.get(NargesStateSchedulerRunORM, {"run_date": run_date, "slot": slot})
            if row is None:
                row = NargesStateSchedulerRunORM(run_date=run_date, slot=slot, status=status, error=error, created_at=datetime.now(UTC))
                session.add(row)
            else:
                row.status = status
                row.error = error
                row.created_at = datetime.now(UTC)

    def has_scheduler_run(self, run_date: str, slot: str) -> bool:
        with self.database.orm.session() as session:
            row = session.get(NargesStateSchedulerRunORM, {"run_date": run_date, "slot": slot})
        return row is not None and row.status == "ok"

    def _active_payload(self) -> dict | None:
        cached = self._get_cached()
        if cached is not None:
            return cached.model_dump(mode="json")
        with self.database.orm.session() as session:
            row = session.scalar(
                select(NargesSelfStateORM)
                .where(NargesSelfStateORM.is_active.is_(True))
                .order_by(NargesSelfStateORM.id.desc())
                .limit(1)
            )
        return json.loads(row.payload) if row else None

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._cached_state = None
            self._cache_expires_at = None

    def _get_cached(self) -> NargesSelfState | None:
        with self._cache_lock:
            if self._cached_state is None or self._cache_expires_at is None:
                return None
            if self._cache_expires_at <= datetime.now(UTC):
                self._cached_state = None
                self._cache_expires_at = None
                return None
            return self._cached_state.model_copy(deep=True)

    def _set_cached(self, state: NargesSelfState) -> None:
        with self._cache_lock:
            self._cached_state = state.model_copy(deep=True)
            self._cache_expires_at = datetime.now(UTC) + self.cache_ttl

    def _audit(self, action: str, decision: str, reason: str | None, before, after) -> None:
        with self.database.orm.session() as session:
            session.add(
                NargesStateAuditLogORM(
                    action=action,
                    decision=decision,
                    reason=reason,
                    before_payload=json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                    after_payload=json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                    created_at=datetime.now(UTC),
                )
            )
