import json
import re
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import func, select

from bot.models.ai import MemorySuggestion
from bot.models.memory import MemoryItem, MemoryKind
from bot.services.debug_service import DebugService
from bot.storage.database import Database
from bot.storage.orm import MemoryAuditLogORM, MemoryORM
from bot.utils.tokens import estimate_tokens


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
    def __init__(
        self,
        database: Database,
        max_active_memories: int = 80,
        max_context_tokens: int = 700,
        debug_service: DebugService | None = None,
    ) -> None:
        self.database = database
        self.max_active_memories = max_active_memories
        self.max_context_tokens = max_context_tokens
        self.debug_service = debug_service

    def list_active(self, user_id: int, limit: int = 20) -> list[MemoryItem]:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MemoryORM)
                .where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.active.is_(True),
                    (MemoryORM.expires_at.is_(None) | (MemoryORM.expires_at > now)),
                )
                .order_by(MemoryORM.importance.desc(), MemoryORM.updated_at.desc())
                .limit(limit)
            ).all()
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
        selected: list[MemoryItem] = []
        used_tokens = 0
        for item in ranked:
            item_tokens = estimate_tokens(f"{item.kind.value}: {item.summary}")
            if selected and used_tokens + item_tokens > self.max_context_tokens:
                continue
            selected.append(item)
            used_tokens += item_tokens
            if len(selected) >= limit:
                break
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
        deleted = False
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row and row.user_id == user_id:
                row.active = False
                row.updated_at = datetime.now(UTC)
                deleted = True
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
                self._debug("memory_rejected", user_id, {"reason": reason, "suggestion": suggestion.model_dump()})
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

    def apply_obvious_user_facts(self, user_id: int, source_message_id: int | None, source_text: str) -> None:
        for suggestion in self._extract_obvious_suggestions(source_text):
            allowed, reason = self._is_allowed(user_id, source_text, suggestion)
            if not allowed:
                self._audit(user_id, None, suggestion.action, "rejected", reason, None, suggestion.model_dump(), source_message_id)
                continue
            self._save(user_id, source_message_id, suggestion, "heuristic")

    def _replace_identity_name(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
    ) -> None:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MemoryORM).where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.kind == "identity",
                    MemoryORM.active.is_(True),
                    MemoryORM.summary.like("User wants to be called %"),
                )
            ).all()
        for row in rows:
            item = self._row_to_memory(row)
            with self.database.orm.session() as session:
                current = session.get(MemoryORM, item.id)
                if current:
                    current.active = False
                    current.updated_at = datetime.now(UTC)
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
        with self.database.orm.session() as session:
            row = MemoryORM(
                user_id=user_id,
                kind=suggestion.kind,
                summary=suggestion.summary.strip(),
                confidence=suggestion.confidence,
                importance=suggestion.importance,
                source_message_id=source_message_id,
                created_at=now,
                updated_at=now,
                last_seen_at=now,
                expires_at=expires_at,
                active=True,
            )
            session.add(row)
            session.flush()
            memory_id = int(row.id)
        self._audit(user_id, memory_id, action, "accepted", "stored", None, suggestion.model_dump(), source_message_id)
        self._debug("memory_saved", user_id, {"memory_id": memory_id, "action": action, "suggestion": suggestion.model_dump()})
        return memory_id

    def _edit_similar(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion) -> None:
        target = self._find_similar(user_id, suggestion.kind, suggestion.summary)
        if target is None:
            self._save(user_id, source_message_id, suggestion, "create")
            return
        before = target.model_dump()
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, target.id)
            if row and row.user_id == user_id:
                row.summary = suggestion.summary.strip()
                row.confidence = suggestion.confidence
                row.importance = suggestion.importance
                row.updated_at = datetime.now(UTC)
        self._audit(user_id, target.id, "edit", "accepted", "updated similar memory", before, suggestion.model_dump(), source_message_id)
        self._debug("memory_edited", user_id, {"memory_id": target.id, "before": before, "after": suggestion.model_dump()})

    def _merge_similar(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion) -> None:
        similar = self._find_all_similar(user_id, suggestion.kind, suggestion.summary)
        if not similar:
            self._save(user_id, source_message_id, suggestion, "create")
            return
        before = [item.model_dump() for item in similar]
        for item in similar:
            with self.database.orm.session() as session:
                row = session.get(MemoryORM, item.id)
                if row and row.user_id == user_id:
                    row.active = False
                    row.updated_at = datetime.now(UTC)
        memory_id = self._save(user_id, source_message_id, suggestion, "merge")
        self._audit(user_id, memory_id, "merge", "accepted", "merged similar memories", before, suggestion.model_dump(), source_message_id)
        self._debug("memory_merged", user_id, {"memory_id": memory_id, "merged": before, "after": suggestion.model_dump()})

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
            with self.database.orm.session() as session:
                row = session.get(MemoryORM, item.id)
                if row and row.user_id == user_id:
                    row.active = False
                    row.updated_at = datetime.now(UTC)
            self._audit(user_id, item.id, "delete", "accepted", reason, item.model_dump(), None, source_message_id)
            self._debug("memory_deleted", user_id, {"memory_id": item.id, "reason": reason})

    def _active_count(self, user_id: int) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count()).select_from(MemoryORM).where(MemoryORM.user_id == user_id, MemoryORM.active.is_(True))
            )
        return int(value or 0)

    def _is_duplicate(self, user_id: int, kind: str, summary: str) -> bool:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MemoryORM.summary).where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.kind == kind,
                    MemoryORM.active.is_(True),
                )
            ).all()
        return any(SequenceMatcher(None, row.lower(), summary.lower()).ratio() > 0.9 for row in rows)

    def _has_simple_contradiction(self, user_id: int, kind: str, summary: str) -> bool:
        if kind not in {"preference", "constraint", "boundary"}:
            return False
        lower = summary.lower()
        negated = any(token in lower for token in ["not ", "don't", "نمی", "دوست ندارد", "نخواهد"])
        positive_text = re.sub(r"\b(not|don't|doesn't)\b", "", lower).replace("نمی", "").replace("دوست ندارد", "دوست دارد")
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MemoryORM.summary).where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.kind == kind,
                    MemoryORM.active.is_(True),
                )
            ).all()
        for row in rows:
            other = row.lower()
            other_negated = any(token in other for token in ["not ", "don't", "نمی", "دوست ندارد", "نخواهد"])
            other_positive = re.sub(r"\b(not|don't|doesn't)\b", "", other).replace("نمی", "").replace("دوست ندارد", "دوست دارد")
            if negated != other_negated and SequenceMatcher(None, positive_text, other_positive).ratio() > 0.78:
                return True
        return False

    def _find_similar(self, user_id: int, kind: str | None, summary: str) -> MemoryItem | None:
        items = self._find_all_similar(user_id, kind, summary)
        return items[0] if items else None

    def _find_all_similar(self, user_id: int, kind: str | None, summary: str) -> list[MemoryItem]:
        filters = [MemoryORM.user_id == user_id, MemoryORM.active.is_(True)]
        if kind:
            filters.append(MemoryORM.kind == kind)
        with self.database.orm.session() as session:
            rows = session.scalars(select(MemoryORM).where(*filters)).all()
        items = [self._row_to_memory(row) for row in rows]
        scored = [
            item for item in items
            if SequenceMatcher(None, item.summary.lower(), summary.lower()).ratio() > 0.58
            or bool(self._keywords(item.summary) & self._keywords(summary))
        ]
        return scored[:5]

    def _get(self, user_id: int, memory_id: int) -> dict | None:
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if not row or row.user_id != user_id:
                return None
            return self._row_to_memory(row).model_dump(mode="json")

    def _touch(self, memory_id: int) -> None:
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row:
                row.last_seen_at = datetime.now(UTC)

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
        with self.database.orm.session() as session:
            session.add(
                MemoryAuditLogORM(
                    user_id=user_id,
                    memory_id=memory_id,
                    action=action,
                    decision=decision,
                    reason=reason,
                    before_payload=json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                    after_payload=json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                    source_message_id=source_message_id,
                    created_at=datetime.now(UTC),
                )
            )

    def _debug(self, event: str, user_id: int, payload: dict) -> None:
        if self.debug_service:
            self.debug_service.log(event, payload, user_id=user_id)

    def _keywords(self, text: str) -> set[str]:
        return {word.lower() for word in re.findall(r"[\w\u0600-\u06FF]{3,}", text or "")}

    def _extract_obvious_suggestions(self, text: str) -> list[MemorySuggestion]:
        text = (text or "").strip()
        if not text or len(text) > 220:
            return []
        patterns = [
            r"(?:من\s+)?(?P<thing>[\w\u0600-\u06FF\s‌-]{2,50})\s+را\s+دوست\s+دارم",
            r"(?:من\s+)?(?P<thing>[\w\u0600-\u06FF\s‌-]{2,50})\s+دوست\s+دارم",
            r"از\s+(?P<thing>[\w\u0600-\u06FF\s‌-]{2,50})\s+خوشم\s+می(?:اد|آید)",
            r"\bi\s+(?:like|love|prefer)\s+(?P<thing>[a-zA-Z0-9\s-]{2,50})",
        ]
        suggestions: list[MemorySuggestion] = []
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            thing = re.sub(r"\s+", " ", match.group("thing")).strip(" .،,!؟")
            if not thing or thing in {"من", "i"}:
                continue
            summary = f"کاربر {thing} را دوست دارد."
            suggestions.append(
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary=summary,
                    confidence=0.86,
                    importance=3,
                )
            )
            break
        return suggestions

    def _row_to_memory(self, row) -> MemoryItem:
        return MemoryItem(
            id=self._value(row, "id"),
            user_id=self._value(row, "user_id"),
            kind=MemoryKind(self._value(row, "kind")),
            summary=self._value(row, "summary"),
            confidence=self._value(row, "confidence"),
            importance=self._value(row, "importance") or 3,
            source_message_id=self._value(row, "source_message_id"),
            created_at=self._dt(self._value(row, "created_at")),
            updated_at=self._dt(self._value(row, "updated_at")),
            last_seen_at=self._dt(self._value(row, "last_seen_at")) if self._value(row, "last_seen_at") else None,
            expires_at=self._dt(self._value(row, "expires_at")) if self._value(row, "expires_at") else None,
            active=bool(self._value(row, "active")),
        )

    def _value(self, row, name: str):
        if hasattr(row, name):
            return getattr(row, name)
        return row[name]

    def _dt(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
