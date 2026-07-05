import json
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from bot.models.ai import MemorySuggestion
from bot.models.memory import MemoryItem, MemoryKind
from bot.storage.database import Database


SENSITIVE_WORDS = [
    "رمز",
    "پسورد",
    "password",
    "token",
    "api key",
    "secret",
    "کد ملی",
    "کارت بانکی",
]
INJECTION_WORDS = [
    "ignore previous",
    "system prompt",
    "developer message",
    "دستورهای قبلی",
    "دستور قبلی",
    "پرامپت سیستم",
]
LOW_VALUE_PATTERNS = [
    r"^(سلام|باشه|اوکی|مرسی|ممنون)$",
    r"^کاربر پیام داد",
]


class MemoryService:
    def __init__(self, database: Database, max_active_memories: int = 80) -> None:
        self.database = database
        self.max_active_memories = max_active_memories

    def list_active(self, user_id: int, limit: int = 20) -> list[MemoryItem]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND active = 1
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY importance DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, datetime.now(UTC).isoformat(), limit),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def retrieve_relevant(self, user_id: int, text: str, limit: int = 12) -> list[MemoryItem]:
        memories = self.list_active(user_id, limit=80)
        query_words = self._keywords(text)

        def score(memory: MemoryItem) -> tuple[int, float, datetime]:
            memory_words = self._keywords(f"{memory.kind.value} {memory.summary}")
            overlap = len(query_words & memory_words)
            similarity = SequenceMatcher(None, text.lower(), memory.summary.lower()).ratio()
            return (overlap + memory.importance, similarity, memory.updated_at)

        ranked = sorted(memories, key=score, reverse=True)
        selected = ranked[:limit]
        for item in selected:
            self._touch(item.id)
        return selected

    def upsert_identity_name(self, user_id: int, name: str, source_message_id: int | None = None) -> None:
        suggestion = MemorySuggestion(
            action="replace",
            kind="identity",
            summary=f"User wants to be called {name}.",
            confidence=1,
            importance=5,
        )
        self._replace_identity_name(user_id, source_message_id, suggestion)

    def delete(self, user_id: int, memory_id: int) -> bool:
        before = self._get(user_id, memory_id)
        with closing(self.database.connect()) as connection:
            cursor = connection.execute(
                "UPDATE memories SET active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (datetime.now(UTC).isoformat(), memory_id, user_id),
            )
            connection.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self._audit(user_id, memory_id, "delete", "accepted", "user requested deletion", before, None, None)
        return deleted

    def apply_suggestions(
        self,
        user_id: int,
        source_message_id: int | None,
        source_text: str,
        suggestions: list[MemorySuggestion],
    ) -> None:
        for suggestion in suggestions:
            allowed, reason = self._is_allowed(user_id, source_text, suggestion)
            if not allowed:
                self._audit(user_id, None, suggestion.action, "rejected", reason, None, suggestion.model_dump(), source_message_id)
                continue

            action = "create" if suggestion.action == "save" else suggestion.action
            if action in {"delete", "forget"}:
                self._forget_similar(user_id, suggestion.summary, source_message_id)
            elif action == "edit":
                self._edit_similar(user_id, source_message_id, suggestion)
            elif action == "merge":
                self._merge_similar(user_id, source_message_id, suggestion)
            elif action == "replace":
                self._replace_similar(user_id, source_message_id, suggestion)
            else:
                self._save(user_id, source_message_id, suggestion, "create")

    def _replace_identity_name(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
    ) -> None:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND kind = 'identity' AND active = 1 AND summary LIKE 'User wants to be called %'
                """,
                (user_id,),
            ).fetchall()
        for row in rows:
            item = self._row_to_memory(row)
            self.database.execute(
                "UPDATE memories SET active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (datetime.now(UTC).isoformat(), item.id, user_id),
            )
            self._audit(user_id, item.id, "replace", "accepted", "replaced display name", item.model_dump(), None, source_message_id)
        self._save(user_id, source_message_id, suggestion, "replace")

    def _is_allowed(self, user_id: int, source_text: str, suggestion: MemorySuggestion) -> tuple[bool, str]:
        summary = suggestion.summary.strip()
        lowered = f"{summary} {source_text}".lower()
        if suggestion.confidence < 0.55:
            return False, "low confidence"
        if len(summary) < 8:
            return False, "too short"
        if any(word in lowered for word in SENSITIVE_WORDS):
            return False, "sensitive content"
        if any(word in lowered for word in INJECTION_WORDS):
            return False, "prompt injection content"
        if any(re.search(pattern, summary, re.IGNORECASE) for pattern in LOW_VALUE_PATTERNS):
            return False, "low value memory"
        if self._active_count(user_id) >= self.max_active_memories and suggestion.action not in {"delete", "forget"}:
            return False, "memory limit reached"
        if self._is_duplicate(user_id, suggestion.kind, summary):
            return False, "duplicate memory"
        if self._has_simple_contradiction(user_id, suggestion.kind, summary):
            return False, "possible contradiction"
        return True, "accepted"

    def _save(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion, action: str) -> int:
        now = datetime.now(UTC)
        expires_at = None
        if suggestion.kind == "temporary_event" or suggestion.expires_in_days:
            days = suggestion.expires_in_days or 14
            expires_at = now + timedelta(days=days)
        with closing(self.database.connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories(
                    user_id, kind, summary, confidence, importance, source_message_id,
                    created_at, updated_at, last_seen_at, expires_at, active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    user_id,
                    suggestion.kind,
                    suggestion.summary.strip(),
                    suggestion.confidence,
                    suggestion.importance,
                    source_message_id,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                ),
            )
            connection.commit()
            memory_id = int(cursor.lastrowid)
        self._audit(user_id, memory_id, action, "accepted", "stored", None, suggestion.model_dump(), source_message_id)
        return memory_id

    def _edit_similar(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion) -> None:
        target = self._find_similar(user_id, suggestion.kind, suggestion.summary)
        if target is None:
            self._save(user_id, source_message_id, suggestion, "create")
            return
        before = target.model_dump()
        self.database.execute(
            """
            UPDATE memories
            SET summary = ?, confidence = ?, importance = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                suggestion.summary.strip(),
                suggestion.confidence,
                suggestion.importance,
                datetime.now(UTC).isoformat(),
                target.id,
                user_id,
            ),
        )
        self._audit(user_id, target.id, "edit", "accepted", "updated similar memory", before, suggestion.model_dump(), source_message_id)

    def _merge_similar(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion) -> None:
        similar = self._find_all_similar(user_id, suggestion.kind, suggestion.summary)
        if not similar:
            self._save(user_id, source_message_id, suggestion, "create")
            return
        before = [item.model_dump() for item in similar]
        for item in similar:
            self.database.execute(
                "UPDATE memories SET active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (datetime.now(UTC).isoformat(), item.id, user_id),
            )
        memory_id = self._save(user_id, source_message_id, suggestion, "merge")
        self._audit(user_id, memory_id, "merge", "accepted", "merged similar memories", before, suggestion.model_dump(), source_message_id)

    def _replace_similar(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion) -> None:
        self._forget_similar(user_id, suggestion.summary, source_message_id, reason="replace old similar memories")
        self._save(user_id, source_message_id, suggestion, "replace")

    def _forget_similar(
        self,
        user_id: int,
        summary: str,
        source_message_id: int | None,
        reason: str = "model suggested deletion",
    ) -> None:
        similar = self._find_all_similar(user_id, None, summary)
        for item in similar:
            self.database.execute(
                "UPDATE memories SET active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
                (datetime.now(UTC).isoformat(), item.id, user_id),
            )
            self._audit(user_id, item.id, "delete", "accepted", reason, item.model_dump(), None, source_message_id)

    def _active_count(self, user_id: int) -> int:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM memories WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchone()
        return int(row["count"])

    def _is_duplicate(self, user_id: int, kind: str, summary: str) -> bool:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT summary FROM memories
                WHERE user_id = ? AND kind = ? AND active = 1
                """,
                (user_id, kind),
            ).fetchall()
        return any(SequenceMatcher(None, row["summary"].lower(), summary.lower()).ratio() > 0.9 for row in rows)

    def _has_simple_contradiction(self, user_id: int, kind: str, summary: str) -> bool:
        if kind not in {"preference", "constraint", "boundary"}:
            return False
        lower = summary.lower()
        negated = any(token in lower for token in ["not ", "don't", "نمی", "دوست ندارد", "نخواهد"])
        positive_text = re.sub(r"\b(not|don't|doesn't)\b", "", lower).replace("نمی", "").replace("دوست ندارد", "دوست دارد")
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                "SELECT summary FROM memories WHERE user_id = ? AND kind = ? AND active = 1",
                (user_id, kind),
            ).fetchall()
        for row in rows:
            other = row["summary"].lower()
            other_negated = any(token in other for token in ["not ", "don't", "نمی", "دوست ندارد", "نخواهد"])
            other_positive = re.sub(r"\b(not|don't|doesn't)\b", "", other).replace("نمی", "").replace("دوست ندارد", "دوست دارد")
            if negated != other_negated and SequenceMatcher(None, positive_text, other_positive).ratio() > 0.78:
                return True
        return False

    def _find_similar(self, user_id: int, kind: str | None, summary: str) -> MemoryItem | None:
        items = self._find_all_similar(user_id, kind, summary)
        return items[0] if items else None

    def _find_all_similar(self, user_id: int, kind: str | None, summary: str) -> list[MemoryItem]:
        query = "SELECT * FROM memories WHERE user_id = ? AND active = 1"
        params: list[object] = [user_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        with closing(self.database.connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        items = [self._row_to_memory(row) for row in rows]
        scored = [
            item for item in items
            if SequenceMatcher(None, item.summary.lower(), summary.lower()).ratio() > 0.58
            or bool(self._keywords(item.summary) & self._keywords(summary))
        ]
        return scored[:5]

    def _get(self, user_id: int, memory_id: int) -> dict | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE user_id = ? AND id = ?",
                (user_id, memory_id),
            ).fetchone()
        return dict(row) if row else None

    def _touch(self, memory_id: int) -> None:
        self.database.execute(
            "UPDATE memories SET last_seen_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), memory_id),
        )

    def _audit(
        self,
        user_id: int,
        memory_id: int | None,
        action: str,
        decision: str,
        reason: str | None,
        before,
        after,
        source_message_id: int | None,
    ) -> None:
        self.database.execute(
            """
            INSERT INTO memory_audit_logs(
                user_id, memory_id, action, decision, reason,
                before_payload, after_payload, source_message_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                memory_id,
                action,
                decision,
                reason,
                json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                source_message_id,
                datetime.now(UTC).isoformat(),
            ),
        )

    def _keywords(self, text: str) -> set[str]:
        return {word.lower() for word in re.findall(r"[\w\u0600-\u06FF]{3,}", text or "")}

    def _row_to_memory(self, row) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            user_id=row["user_id"],
            kind=MemoryKind(row["kind"]),
            summary=row["summary"],
            confidence=row["confidence"],
            importance=row["importance"] if "importance" in row.keys() else 3,
            source_message_id=row["source_message_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None,
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            active=bool(row["active"]),
        )
