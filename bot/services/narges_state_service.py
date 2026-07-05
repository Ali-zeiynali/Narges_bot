import json
import re
from contextlib import closing
from datetime import UTC, datetime

from bot.models.state import NargesSelfState, NargesSelfStateCandidate
from bot.storage.database import Database


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
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_active(self) -> NargesSelfState:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM narges_self_states WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
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
            return state
        return NargesSelfState.model_validate(json.loads(row["payload"]))

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
        with closing(self.database.connect()) as connection:
            connection.execute("UPDATE narges_self_states SET is_active = 0 WHERE is_active = 1")
            connection.execute(
                """
                INSERT INTO narges_self_states(
                    payload, mood, energy, activity, location, is_alone,
                    companions, mind_topics, source, is_active, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    state.model_dump_json(),
                    state.mood,
                    state.energy,
                    state.activity,
                    state.location,
                    int(state.is_alone),
                    json.dumps(state.companions, ensure_ascii=False),
                    json.dumps(state.mind_topics, ensure_ascii=False),
                    source,
                    now.isoformat(),
                ),
            )
            connection.commit()
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
        self.database.execute(
            """
            INSERT INTO narges_state_scheduler_runs(run_date, slot, status, error, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_date, slot) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                created_at = excluded.created_at
            """,
            (run_date, slot, status, error, datetime.now(UTC).isoformat()),
        )

    def has_scheduler_run(self, run_date: str, slot: str) -> bool:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT status FROM narges_state_scheduler_runs WHERE run_date = ? AND slot = ? AND status = 'ok'",
                (run_date, slot),
            ).fetchone()
        return row is not None

    def _active_payload(self) -> dict | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM narges_self_states WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def _audit(self, action: str, decision: str, reason: str | None, before, after) -> None:
        self.database.execute(
            """
            INSERT INTO narges_state_audit_logs(action, decision, reason, before_payload, after_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                decision,
                reason,
                json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                datetime.now(UTC).isoformat(),
            ),
        )
