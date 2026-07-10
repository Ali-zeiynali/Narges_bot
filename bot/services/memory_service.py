from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from sqlalchemy import and_, func, select, update

from bot.models.ai import MemorySuggestion
from bot.models.memory import MemoryItem, MemoryKind
from bot.services.debug_service import DebugService
from bot.storage.database import Database
from bot.storage.orm import MemoryAuditLogORM, MemoryORM
from bot.utils.tokens import estimate_tokens


SENSITIVE_WORDS = (
    "رمز",
    "پسورد",
    "password",
    "token",
    "api key",
    "secret",
    "کد ملی",
    "شماره کارت",
    "cvv",
)
INJECTION_WORDS = (
    "ignore previous",
    "system prompt",
    "developer message",
    "دستورهای قبلی",
    "دستور قبلی",
    "پرامپت سیستم",
)
LOW_VALUE_PATTERNS = (
    r"^(سلام|باشه|اوکی|مرسی|ممنون|خوبم|اره|آره|نه|لول|خخخ)$",
    r"^کاربر پیام داد",
)
TEMPORARY_WORDS = ("امروز", "امشب", "فعلاً", "فعلا", "این هفته", "الان", "حالم", "خسته‌ام", "استرس دارم")
STABLE_MARKERS = ("همیشه", "معمولاً", "معمولا", "از این به بعد", "ترجیح میدم", "ترجیح می‌دم", "هدفم", "کارم", "شغلم")
AMBIGUOUS_USER_TEXTS = {
    "حدس بزن",
    "خب",
    "باشه",
    "اوکی",
    "نمیگم",
    "نمی‌گم",
    "هیچی",
    "محرمانه",
    "خصوصی",
    "مرسی",
    "ممنون",
    "اره",
    "آره",
    "نه",
    "guess",
    "ok",
    "thanks",
    "nothing",
}
UNCERTAIN_SUMMARY_MARKERS = ("احتمالاً", "احتمالا", "شاید", "به نظر می‌رسد", "به نظر میرسد", "probably", "maybe", "seems")


@dataclass(frozen=True)
class MemoryDecision:
    accepted: bool
    reason: str


class MemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_active(self, user_id: int, limit: int = 80) -> list[MemoryItem]:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MemoryORM)
                .where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.active.is_(True),
                    MemoryORM.expires_at.is_(None) | (MemoryORM.expires_at > now),
                )
                .order_by(MemoryORM.importance.desc(), MemoryORM.updated_at.desc())
                .limit(limit)
            ).all()
        return [self.row_to_memory(row) for row in rows]

    def active_count(self, user_id: int) -> int:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count())
                .select_from(MemoryORM)
                .where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.active.is_(True),
                    MemoryORM.expires_at.is_(None) | (MemoryORM.expires_at > now),
                )
            )
        return int(value or 0)

    def prune_stale(self, user_id: int, days: int = 180) -> int:
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=days)
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(MemoryORM).where(
                    MemoryORM.user_id == user_id,
                    MemoryORM.active.is_(True),
                    (MemoryORM.expires_at.is_not(None) & (MemoryORM.expires_at <= now))
                    | and_(
                        MemoryORM.importance <= 2,
                        MemoryORM.updated_at < cutoff,
                        MemoryORM.last_seen_at.is_(None) | (MemoryORM.last_seen_at < cutoff),
                    ),
                )
            ).all()
            for row in rows:
                row.active = False
                row.updated_at = now
            return len(rows)

    def save(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion, action: str) -> int:
        now = datetime.now(UTC)
        expires_at = self._expires_at(suggestion, now)
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
            return int(row.id)

    def update(self, memory_id: int, suggestion: MemorySuggestion) -> MemoryItem | None:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row is None:
                return None
            row.summary = suggestion.summary.strip()
            row.kind = suggestion.kind
            row.confidence = suggestion.confidence
            row.importance = suggestion.importance
            row.expires_at = self._expires_at(suggestion, now)
            row.updated_at = now
            row.last_seen_at = now
            row.active = True
            session.flush()
            return self.row_to_memory(row)

    def deactivate(self, memory_id: int) -> MemoryItem | None:
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row is None:
                return None
            item = self.row_to_memory(row)
            row.active = False
            row.updated_at = datetime.now(UTC)
            return item

    def touch_many(self, memory_ids: list[int]) -> None:
        ids = sorted({int(memory_id) for memory_id in memory_ids if memory_id})
        if not ids:
            return
        with self.database.orm.session() as session:
            session.execute(
                update(MemoryORM)
                .where(MemoryORM.id.in_(ids))
                .values(last_seen_at=datetime.now(UTC))
            )

    def touch(self, memory_id: int) -> None:
        self.touch_many([memory_id])

    def get(self, user_id: int, memory_id: int) -> MemoryItem | None:
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row is None or row.user_id != user_id:
                return None
            return self.row_to_memory(row)

    def row_to_memory(self, row) -> MemoryItem:
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

    def _expires_at(self, suggestion: MemorySuggestion, now: datetime) -> datetime | None:
        expires_in_days = getattr(suggestion, "expires_in_days", None)
        if suggestion.kind == "temporary_event" and not expires_in_days:
            expires_in_days = 7
        if not expires_in_days:
            return None
        return now + timedelta(days=max(1, min(int(expires_in_days), 365)))

    def _value(self, row, name: str):
        return getattr(row, name) if hasattr(row, name) else row[name]

    def _dt(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class MemoryAuditService:
    def __init__(self, database: Database, debug_service: DebugService | None = None) -> None:
        self.database = database
        self.debug_service = debug_service

    def log(
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
                    before_payload=self._json(before),
                    after_payload=self._json(after),
                    source_message_id=source_message_id,
                    created_at=datetime.now(UTC),
                )
            )

    def debug(self, event: str, user_id: int, payload: dict) -> None:
        if self.debug_service:
            self.debug_service.log(event, payload, user_id=user_id)

    def _json(self, value) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class MemoryExtractor:
    def extract(self, user_text: str, existing_memories: list[MemoryItem], metadata: dict | None = None) -> list[MemorySuggestion]:
        text = self._compact(user_text)
        if not text or len(text) > 700 or self._is_low_value(text) or self._contains_injection(text):
            return []
        candidates: list[MemorySuggestion] = []
        candidates.extend(self._extract_explicit_forget(text, existing_memories))
        candidates.extend(self._extract_name(text))
        candidates.extend(self._extract_explicit_save(text))
        candidates.extend(self._extract_stable_preference(text))
        candidates.extend(self._extract_stable_fact(text))
        return self._dedupe(candidates)[:4]

    def _extract_explicit_forget(self, text: str, existing: list[MemoryItem]) -> list[MemorySuggestion]:
        lowered = text.lower()
        if not any(word in lowered for word in ("فراموش کن", "حذفش کن", "از حافظه پاک کن", "forget", "remove from memory")):
            return []
        target_text = re.sub(r"^(لطفاً\s*)?(این(و| رو)?\s*)?", "", text).strip()
        target = self._best_existing_target(target_text, existing)
        return [
            MemorySuggestion(
                action="delete",
                kind=target.kind.value if target else "fact",
                summary=target.summary if target else target_text[:180],
                memory_id=target.id if target else None,
                confidence=1.0,
                importance=1,
            )
        ]

    def _extract_name(self, text: str) -> list[MemorySuggestion]:
        patterns = (
            r"(?:اسمم|اسم من)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})\s*(?:است|ه)?$",
            r"(?:منو|مرا|من را)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})\s+صدا\s+(?:کن|بزن)",
            r"(?:صدام کن|صدایم کن)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})",
            r"\bcall me (?P<name>[a-zA-Z][a-zA-Z0-9 _-]{1,40})",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            name = self._compact(match.group("name")).strip(" .،,!؟")
            return [
                MemorySuggestion(
                    action="replace",
                    kind="identity",
                    summary=f"نام ترجیحی کاربر: {name}",
                    confidence=1.0,
                    importance=5,
                )
            ]
        return []

    def _extract_explicit_save(self, text: str) -> list[MemorySuggestion]:
        match = re.search(r"(?:یادت باشه|یادداشت کن|ذخیره کن|remember(?: that)?)\s*[:،-]?\s*(?P<value>.+)", text, flags=re.IGNORECASE)
        if not match:
            return []
        value = self._compact(match.group("value"))[:220]
        if not value or self._contains_sensitive(value):
            return []
        kind = self._infer_kind(value)
        expires = 7 if any(word in value for word in TEMPORARY_WORDS) else None
        return [
            MemorySuggestion(
                action="create",
                kind="temporary_event" if expires else kind,
                summary=value,
                confidence=1.0,
                importance=4,
                expires_in_days=expires,
            )
        ]

    def _extract_stable_preference(self, text: str) -> list[MemorySuggestion]:
        english_match = re.search(r"\bI\s+(?:like|love|prefer)\s+(?P<value>.+)", text, flags=re.IGNORECASE)
        if english_match:
            value = self._clean_value(english_match.group("value"))
            if value:
                return [
                    MemorySuggestion(
                        action="create",
                        kind="preference",
                        summary=f"User likes {value}.",
                        confidence=0.9,
                        importance=4,
                    )
                ]
        persian_like = re.search(r"دوست دارم\s+(?P<value>.+?)(?:\s+و\s+|$)", text, flags=re.IGNORECASE)
        if persian_like:
            value = self._clean_value(persian_like.group("value"))
            if value:
                return [
                    MemorySuggestion(
                        action="create",
                        kind="preference",
                        summary=f"ترجیح کاربر: {value}",
                        confidence=0.9,
                        importance=4,
                    )
                ]
        patterns = (
            r"(?:از این به بعد\s+)?(?:ترجیح می‌?دم|ترجیح میدم)\s+(?P<value>.+)",
            r"(?:همیشه\s+)?دوست دارم\s+(?P<value>.+)",
            r"(?:همیشه\s+)?از\s+(?P<value>.+?)\s+خوشم\s+می(?:اد|آید)",
        )
        if not any(marker in text for marker in STABLE_MARKERS):
            return []
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = self._clean_value(match.group("value"))
            if not value:
                return []
            return [
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary=f"ترجیح کاربر: {value}",
                    confidence=0.9,
                    importance=4,
                )
            ]
        return []

    def _extract_stable_fact(self, text: str) -> list[MemorySuggestion]:
        results: list[MemorySuggestion] = []
        name_match = re.search(r"(?:اسمم|اسم من)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})\s*(?:است|ه)?", text, flags=re.IGNORECASE)
        if name_match:
            name = self._clean_value(name_match.group("name"))
            if name:
                results.append(
                    MemorySuggestion(
                        action="replace",
                        kind="identity",
                        summary=f"نام ترجیحی کاربر: {name}",
                        confidence=1.0,
                        importance=5,
                    )
                )
        if "شوخی" in text:
            results.append(
                MemorySuggestion(
                    action="create",
                    kind="inside_joke",
                    summary="کاربر دوست دارد گفتگو حالت شوخی داشته باشد.",
                    confidence=0.85,
                    importance=3,
                )
            )
        if results:
            return results
        patterns = (
            (r"(?:کارم|شغلم)\s+(?P<value>.+?)(?:\s+است|\s+ه)?$", "identity", 4),
            (r"(?:روی|در)\s+(?P<value>.+?)\s+کار\s+می(?:کنم|کنم)$", "project", 4),
            (r"هدفم\s+(?P<value>.+)$", "goal", 4),
            (r"از این به بعد\s+(?P<value>.+)$", "constraint", 4),
        )
        for pattern, kind, importance in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = self._clean_value(match.group("value"))
            if not value:
                continue
            return [
                MemorySuggestion(
                    action="create",
                    kind=kind,
                    summary=value,
                    confidence=0.9,
                    importance=importance,
                )
            ]
        return []

    def _best_existing_target(self, text: str, existing: list[MemoryItem]) -> MemoryItem | None:
        scored = [(MemoryDeduplicator.similarity(text, item.summary), item) for item in existing]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] >= 0.45 else None

    def _infer_kind(self, value: str) -> str:
        lowered = value.lower()
        if any(word in lowered for word in ("هدف", "می‌خوام", "میخوام", "goal")):
            return "goal"
        if any(word in lowered for word in ("پروژه", "دارم می‌سازم", "کار می‌کنم", "project")):
            return "project"
        if any(word in lowered for word in ("دوست دارم", "ترجیح", "prefer")):
            return "preference"
        if any(word in lowered for word in ("نباید", "نمی‌خوام", "نمیخوام", "do not")):
            return "constraint"
        return "fact"

    def _clean_value(self, value: str) -> str | None:
        compact = self._compact(value).strip(" .،,!؟")
        if not compact or len(compact) > 220 or len(compact.split()) > 28:
            return None
        if self._contains_sensitive(compact) or self._contains_injection(compact):
            return None
        return compact

    def _is_low_value(self, text: str) -> bool:
        return any(re.fullmatch(pattern, text, flags=re.IGNORECASE) for pattern in LOW_VALUE_PATTERNS)

    def _contains_sensitive(self, text: str) -> bool:
        lowered = text.lower()
        return any(word.lower() in lowered for word in SENSITIVE_WORDS)

    def _contains_injection(self, text: str) -> bool:
        lowered = text.lower()
        return any(word.lower() in lowered for word in INJECTION_WORDS)

    def _compact(self, text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _dedupe(self, candidates: list[MemorySuggestion]) -> list[MemorySuggestion]:
        seen: set[tuple[str, str]] = set()
        result: list[MemorySuggestion] = []
        for candidate in candidates:
            key = (candidate.kind, MemoryDeduplicator.normalize(candidate.summary))
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
        return result


class MemoryPolicyGate:
    def __init__(self, repository: MemoryRepository, max_active_memories: int) -> None:
        self.repository = repository
        self.max_active_memories = max_active_memories

    def decide(
        self,
        user_id: int,
        source_text: str,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
        *,
        assistant_sourced: bool = False,
        model_sourced: bool = False,
        metadata: dict | None = None,
    ) -> MemoryDecision:
        action = self.normalize_action(suggestion.action)
        summary = self._compact(suggestion.summary)
        source = self._compact(source_text)
        intent = str((metadata or {}).get("intent") or "")
        if assistant_sourced:
            return MemoryDecision(False, "assistant sourced memory is disabled")
        if action == "create" and len(existing) >= self.max_active_memories:
            return MemoryDecision(False, "memory limit reached")
        if action in {"create", "edit", "merge", "replace"} and not summary:
            return MemoryDecision(False, "empty memory")
        if len(summary) > 260:
            return MemoryDecision(False, "memory too long")
        if self._contains_sensitive(summary) or self._contains_injection(summary):
            return MemoryDecision(False, "unsafe memory")
        if any(marker in summary.lower() for marker in UNCERTAIN_SUMMARY_MARKERS):
            return MemoryDecision(False, "uncertain memory")
        if self._looks_like_raw_dialogue(summary):
            return MemoryDecision(False, "raw dialogue is not a memory")
        if action in {"delete", "forget"}:
            if suggestion.memory_id or self._is_explicit_forget(source):
                return MemoryDecision(True, "explicit forget")
            return MemoryDecision(False, "forget request not explicit")
        if action in {"edit", "merge", "replace"} and suggestion.memory_id:
            return MemoryDecision(True, "targeted update")
        if self._is_explicit_save(source):
            return MemoryDecision(True, "explicit save")
        if intent in {"guessing", "continuation"}:
            return MemoryDecision(False, f"blocked for {intent}")
        if self._is_ambiguous_source(source):
            return MemoryDecision(False, "ambiguous source")
        if model_sourced:
            if float(suggestion.confidence or 0) < 0.9:
                return MemoryDecision(False, "model confidence too low")
            if not self._model_source_supports(source, summary, suggestion.kind):
                return MemoryDecision(False, "model memory unsupported")
        elif self._source_supports(source, summary, suggestion.kind):
            return MemoryDecision(True, "source supported")
        if action == "create" and self._duplicate(existing, suggestion):
            return MemoryDecision(True, "duplicate update")
        if not model_sourced and not self._looks_stable(source, suggestion):
            return MemoryDecision(False, "not stable enough")
        return MemoryDecision(True, "accepted")

    def normalize_action(self, action: str) -> str:
        normalized = (action or "create").strip().lower()
        return "create" if normalized == "save" else normalized

    def _duplicate(self, existing: list[MemoryItem], suggestion: MemorySuggestion) -> bool:
        return MemoryDeduplicator.find_in(existing, suggestion) is not None

    def _looks_like_raw_dialogue(self, summary: str) -> bool:
        return "\n" in summary or summary.count(":") >= 3 or len(summary.split()) > 34

    def _is_ambiguous_source(self, source: str) -> bool:
        normalized = re.sub(r"[؟?!.,،\s]+", " ", source.lower()).strip()
        return normalized in AMBIGUOUS_USER_TEXTS or len(normalized) < 8

    def _contains_sensitive(self, value: str) -> bool:
        lowered = value.lower()
        return any(word.lower() in lowered for word in SENSITIVE_WORDS)

    def _contains_injection(self, value: str) -> bool:
        lowered = value.lower()
        return any(word.lower() in lowered for word in INJECTION_WORDS)

    def _is_explicit_forget(self, source: str) -> bool:
        return any(word in source.lower() for word in ("فراموش کن", "حذف کن", "از حافظه پاک کن", "forget", "remove from memory"))

    def _is_explicit_save(self, source: str) -> bool:
        lowered = source.lower().strip()
        if any(word in lowered for word in ("یادت باشه", "یادداشت کن", "ذخیره کن", "remember that", "save this", "save")):
            return True
        return any(word in source.lower() for word in ("یادت باشه", "یادداشت کن", "ذخیره کن", "remember that", "save this"))

    def _looks_stable(self, source: str, suggestion: MemorySuggestion) -> bool:
        if suggestion.kind == "temporary_event" or getattr(suggestion, "expires_in_days", None):
            return True
        if suggestion.kind == "identity":
            return True
        return any(marker in source.lower() for marker in STABLE_MARKERS)

    def _source_supports(self, source: str, summary: str, kind: str) -> bool:
        source_words = self._keywords(source)
        summary_words = self._keywords(summary)
        if len(source_words & summary_words) >= 2:
            return True
        lowered_source = source.lower()
        if kind == "preference" and any(marker in lowered_source for marker in ("i like ", "i love ", "i prefer ", "دوست دارم", "ترجیح")):
            return True
        if kind == "inside_joke" and any(marker in lowered_source for marker in ("شوخی", "joke", "tease")):
            return True
        markers = {
            "identity": ("اسم", "صدام", "کارم", "شغلم", "name", "call me"),
            "preference": ("ترجیح", "دوست دارم", "خوشم میاد", "prefer"),
            "project": ("پروژه", "کار می‌کنم", "می‌سازم", "project"),
            "goal": ("هدف", "می‌خوام", "goal"),
            "constraint": ("از این به بعد", "نباید", "نمی‌خوام", "do not"),
        }
        return any(marker in source.lower() for marker in markers.get(kind, ()))

    def _model_source_supports(self, source: str, summary: str, kind: str) -> bool:
        source_words = self._keywords(source)
        summary_words = self._keywords(summary)
        overlap = source_words & summary_words
        if len(overlap) >= 2:
            return True
        lowered_source = source.lower()
        lowered_summary = summary.lower()
        if kind == "preference" and overlap and any(marker in lowered_source for marker in ("i like ", "i love ", "i prefer ")):
            return any(marker in lowered_summary for marker in ("likes", "loves", "prefers", "prefer"))
        return False

    def _keywords(self, value: str) -> set[str]:
        stop = {"کاربر", "ترجیح", "هدف", "است", "دارد", "برای", "user", "the", "and"}
        return {word for word in re.findall(r"[\w\u0600-\u06FF‌]{3,}", value.lower()) if word not in stop}

    def _compact(self, value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip())


class MemoryDeduplicator:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def find_target(self, existing: list[MemoryItem], suggestion: MemorySuggestion) -> MemoryItem | None:
        return self.find_in(existing, suggestion)

    @staticmethod
    def find_in(existing: list[MemoryItem], suggestion: MemorySuggestion) -> MemoryItem | None:
        canonical = MemoryDeduplicator.canonical_key(suggestion.kind, suggestion.summary)
        same_kind = [item for item in existing if item.kind.value == suggestion.kind]
        for item in same_kind:
            if MemoryDeduplicator.canonical_key(item.kind.value, item.summary) == canonical:
                return item
        scored = [(MemoryDeduplicator.similarity(item.summary, suggestion.summary), item) for item in same_kind]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] >= 0.9 else None

    @staticmethod
    def canonical_key(kind: str, summary: str) -> str:
        normalized = MemoryDeduplicator.normalize(summary)
        normalized = re.sub(r"^(نام ترجیحی کاربر|ترجیح کاربر)\s*:\s*", "", normalized)
        return f"{kind}:{normalized.strip(' .')}"

    @staticmethod
    def normalize(text: str) -> str:
        normalized = (text or "").lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def similarity(left: str, right: str) -> float:
        return SequenceMatcher(None, MemoryDeduplicator.normalize(left), MemoryDeduplicator.normalize(right)).ratio()


class MemoryRetriever:
    def __init__(self, repository: MemoryRepository, max_context_tokens: int) -> None:
        self.repository = repository
        self.max_context_tokens = max(80, int(max_context_tokens))

    def retrieve(
        self,
        user_id: int,
        text: str,
        limit: int = 10,
        *,
        intent: str = "unknown",
        pending_user_thread: str = "",
    ) -> list[MemoryItem]:
        memories = self.repository.list_active(user_id, limit=80)
        query = " ".join(part for part in (text, pending_user_thread) if part)
        query_keywords = self._keywords(query)
        scored: list[tuple[float, MemoryItem]] = []
        for item in memories:
            score = self._score(item, query_keywords, intent)
            if score <= 0:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].importance, pair[1].updated_at), reverse=True)
        selected = self._fit_budget([item for _score, item in scored], limit)
        self.repository.touch_many([item.id for item in selected])
        return selected

    def retrieve_all_for_context(
        self,
        user_id: int,
        limit: int = 40,
        *,
        text: str = "",
        intent: str = "unknown",
        pending_user_thread: str = "",
    ) -> list[MemoryItem]:
        if text or pending_user_thread:
            return self.retrieve(user_id, text, limit=limit, intent=intent, pending_user_thread=pending_user_thread)
        memories = self.repository.list_active(user_id, limit=80)
        selected = self._fit_budget(memories, limit)
        self.repository.touch_many([item.id for item in selected])
        return selected

    def _fit_budget(self, memories: list[MemoryItem], limit: int) -> list[MemoryItem]:
        selected: list[MemoryItem] = []
        used = 0
        for item in memories:
            cost = estimate_tokens(f"{item.kind.value}: {item.summary}")
            if cost > self.max_context_tokens:
                continue
            if selected and used + cost > self.max_context_tokens:
                continue
            selected.append(item)
            used += cost
            if len(selected) >= limit:
                break
        return selected

    def _keywords(self, text: str) -> set[str]:
        normalized = (text or "").lower().replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
        stop = {"این", "اون", "برای", "کاربر", "چی", "چرا", "the", "and", "with", "what"}
        return {word for word in re.findall(r"[\w\u0600-\u06FF]{3,}", normalized) if word not in stop}

    def _score(self, item: MemoryItem, query_keywords: set[str], intent: str) -> float:
        overlap = len(query_keywords & self._keywords(item.summary))
        score = float(overlap * 4)
        if item.importance >= 5:
            score += 1.0
        if intent == "technical" and item.kind.value in {"project", "constraint", "goal"}:
            score += 2.0
        if intent == "support" and item.kind.value in {"boundary", "preference", "user_state"}:
            score += 1.0
        if intent in {"guessing", "continuation"} and overlap == 0:
            return 0.0
        if not query_keywords and item.importance < 5:
            return 0.0
        return score


class MemoryService:
    def __init__(
        self,
        database: Database,
        max_active_memories: int = 160,
        max_context_tokens: int = 700,
        debug_service: DebugService | None = None,
    ) -> None:
        self.repository = MemoryRepository(database)
        self.audit = MemoryAuditService(database, debug_service)
        self.extractor = MemoryExtractor()
        self.policy = MemoryPolicyGate(self.repository, max_active_memories)
        self.deduplicator = MemoryDeduplicator(self.repository)
        self.retriever = MemoryRetriever(self.repository, max_context_tokens)

    def list_active(self, user_id: int, limit: int = 20) -> list[MemoryItem]:
        return self.repository.list_active(user_id, limit)

    def retrieve_relevant(
        self,
        user_id: int,
        text: str,
        limit: int = 6,
        *,
        intent: str = "unknown",
        pending_user_thread: str = "",
    ) -> list[MemoryItem]:
        return self.retriever.retrieve(user_id, text, limit, intent=intent, pending_user_thread=pending_user_thread)

    def retrieve_for_context(
        self,
        user_id: int,
        text: str = "",
        limit: int = 8,
        *,
        intent: str = "unknown",
        pending_user_thread: str = "",
    ) -> list[MemoryItem]:
        return self.retriever.retrieve_all_for_context(
            user_id,
            limit,
            text=text,
            intent=intent,
            pending_user_thread=pending_user_thread,
        )

    def process_user_message(
        self,
        user_id: int,
        source_message_id: int | None,
        user_text: str,
        metadata: dict | None = None,
    ) -> None:
        existing = self.repository.list_active(user_id, limit=80)
        candidates = self.extractor.extract(user_text, existing, metadata)
        self.apply_candidates(
            user_id,
            source_message_id,
            user_text,
            candidates,
            assistant_sourced=False,
            model_sourced=False,
            metadata=metadata,
        )

    def process_model_suggestions(
        self,
        user_id: int,
        source_message_id: int | None,
        user_text: str,
        suggestions: list[MemorySuggestion],
        metadata: dict | None = None,
    ) -> None:
        self.apply_candidates(
            user_id,
            source_message_id,
            user_text,
            suggestions,
            assistant_sourced=False,
            model_sourced=True,
            metadata=metadata,
        )

    def apply_candidates(
        self,
        user_id: int,
        source_message_id: int | None,
        source_text: str,
        suggestions: list[MemorySuggestion],
        *,
        assistant_sourced: bool,
        model_sourced: bool = False,
        metadata: dict | None = None,
    ) -> None:
        if not suggestions:
            return
        existing = self.repository.list_active(user_id, limit=80)
        for suggestion in suggestions[:6]:
            action = self.policy.normalize_action(suggestion.action)
            decision = self.policy.decide(
                user_id,
                source_text,
                suggestion,
                existing,
                assistant_sourced=assistant_sourced,
                model_sourced=model_sourced,
                metadata=metadata,
            )
            if not decision.accepted:
                self.audit.log(user_id, None, action, "rejected", decision.reason, None, suggestion.model_dump(), source_message_id)
                self.audit.debug("memory_rejected", user_id, {"reason": decision.reason, "suggestion": suggestion.model_dump()})
                continue
            if action in {"delete", "forget"}:
                self._delete_similar(user_id, source_message_id, suggestion, existing)
            elif action in {"edit", "merge", "replace"}:
                self._upsert_existing(user_id, source_message_id, suggestion, existing, action)
            else:
                self._create_or_update_duplicate(user_id, source_message_id, suggestion, existing)
            existing = self.repository.list_active(user_id, limit=80)

    def upsert_identity_name(self, user_id: int, name: str, source_message_id: int | None = None) -> None:
        suggestion = MemorySuggestion(
            action="replace",
            kind="identity",
            summary=f"نام ترجیحی کاربر: {name.strip()}",
            confidence=1.0,
            importance=5,
        )
        self.apply_candidates(
            user_id,
            source_message_id,
            f"صدام کن {name}",
            [suggestion],
            assistant_sourced=False,
        )

    def delete(self, user_id: int, memory_id: int) -> bool:
        before = self.repository.get(user_id, memory_id)
        if before is None:
            return False
        deleted = self.repository.deactivate(memory_id)
        self.audit.log(user_id, memory_id, "delete", "accepted", "user requested deletion", before.model_dump(), None, None)
        return deleted is not None

    def prune_stale(self, user_id: int, days: int = 180) -> int:
        return self.repository.prune_stale(user_id, days)

    def _create_or_update_duplicate(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
    ) -> None:
        target = self.deduplicator.find_target(existing, suggestion)
        if target:
            before = target.model_dump(mode="json")
            after = self.repository.update(target.id, suggestion)
            self.audit.log(
                user_id,
                target.id,
                "edit",
                "accepted",
                "updated duplicate memory",
                before,
                after.model_dump(mode="json") if after else None,
                source_message_id,
            )
            return
        memory_id = self.repository.save(user_id, source_message_id, suggestion, "create")
        self.audit.log(user_id, memory_id, "create", "accepted", "stored", None, suggestion.model_dump(), source_message_id)
        self.audit.debug("memory_saved", user_id, {"memory_id": memory_id})

    def _upsert_existing(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
        action: str,
    ) -> None:
        target = self.repository.get(user_id, suggestion.memory_id) if suggestion.memory_id else None
        if target is None:
            target = self.deduplicator.find_target(existing, suggestion)
        if target is None:
            memory_id = self.repository.save(user_id, source_message_id, suggestion, "create")
            self.audit.log(user_id, memory_id, "create", "accepted", f"{action} target not found; created", None, suggestion.model_dump(), source_message_id)
            return
        before = target.model_dump(mode="json")
        after = self.repository.update(target.id, suggestion)
        self.audit.log(
            user_id,
            target.id,
            action,
            "accepted",
            "updated matching memory",
            before,
            after.model_dump(mode="json") if after else None,
            source_message_id,
        )

    def _delete_similar(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
    ) -> None:
        target = self.repository.get(user_id, suggestion.memory_id) if suggestion.memory_id else None
        if target is None:
            target = self.deduplicator.find_target(existing, suggestion)
        if target is None:
            self.audit.log(user_id, None, "delete", "rejected", "matching memory not found", None, suggestion.model_dump(), source_message_id)
            return
        before = target.model_dump(mode="json")
        self.repository.deactivate(target.id)
        self.audit.log(user_id, target.id, "delete", "accepted", "deleted matching memory", before, None, source_message_id)
