import json
import re
from dataclasses import dataclass
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
    "شماره کارت",
    "cvv",
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
    r"^(سلام|باشه|اوکی|مرسی|ممنون|خوبم|اره|آره|نه|لول|خخخ)$",
    r"^کاربر پیام داد",
]
TEMPORARY_WORDS = ["امروز", "امشب", "فعلا", "حالم", "ناراحتم", "خستم", "استرس دارم"]


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
                    (MemoryORM.expires_at.is_(None) | (MemoryORM.expires_at > now)),
                )
                .order_by(MemoryORM.importance.desc(), MemoryORM.updated_at.desc())
                .limit(limit)
            ).all()
        return [self.row_to_memory(row) for row in rows]

    def active_count(self, user_id: int) -> int:
        with self.database.orm.session() as session:
            value = session.scalar(
                select(func.count()).select_from(MemoryORM).where(MemoryORM.user_id == user_id, MemoryORM.active.is_(True))
            )
        return int(value or 0)

    def save(self, user_id: int, source_message_id: int | None, suggestion: MemorySuggestion, action: str) -> int:
        now = datetime.now(UTC)
        expires_at = None
        if suggestion.kind == "temporary_event" or suggestion.expires_in_days:
            expires_at = now + timedelta(days=suggestion.expires_in_days or 14)
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
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row is None:
                return None
            row.summary = suggestion.summary.strip()
            row.kind = suggestion.kind
            row.confidence = suggestion.confidence
            row.importance = suggestion.importance
            row.updated_at = datetime.now(UTC)
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

    def touch(self, memory_id: int) -> None:
        with self.database.orm.session() as session:
            row = session.get(MemoryORM, memory_id)
            if row:
                row.last_seen_at = datetime.now(UTC)

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

    def _value(self, row, name: str):
        if hasattr(row, name):
            return getattr(row, name)
        return row[name]

    def _dt(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed


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
                    before_payload=json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
                    after_payload=json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                    source_message_id=source_message_id,
                    created_at=datetime.now(UTC),
                )
            )

    def debug(self, event: str, user_id: int, payload: dict) -> None:
        if self.debug_service:
            self.debug_service.log(event, payload, user_id=user_id)


class MemoryExtractor:
    def extract(self, user_text: str, existing_memories: list[MemoryItem], metadata: dict | None = None) -> list[MemorySuggestion]:
        text = (user_text or "").strip()
        if not text or len(text) > 600:
            return []
        candidates: list[MemorySuggestion] = []
        candidates.extend(self._extract_name(text))
        candidates.extend(self._extract_preferences(text))
        candidates.extend(self._extract_relationship_style(text))
        candidates.extend(self._extract_stable_facts(text))
        candidates.extend(self._extract_project_goal_constraint(text))
        return self._dedupe_candidates(candidates)[:6]

    def _extract_name(self, text: str) -> list[MemorySuggestion]:
        patterns = [
            r"(?:اسمم|اسم من)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})\s*(?:است|ه)?",
            r"(?:منو|مرا|من را)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})\s+صدا\s+(?:کن|بزن)",
            r"(?:صدام کن|صدایم کن)\s+(?P<name>[\w\u0600-\u06FF‌-]{2,40})",
            r"\bcall me (?P<name>[a-zA-Z][a-zA-Z0-9 _-]{1,40})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                name = re.sub(r"\s+", " ", match.group("name")).strip(" .،,!؟")
                return [
                    MemorySuggestion(
                        action="replace",
                        kind="identity",
                        summary=f"User wants to be called {name}.",
                        confidence=0.95,
                        importance=5,
                    )
                ]
        return []

    def _extract_preferences(self, text: str) -> list[MemorySuggestion]:
        patterns = [
            r"(?:من\s+)?(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})\s+را\s+دوست\s+دارم",
            r"(?:^|[،.!؟]\s*)(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})\s+دوست\s+دارم",
            r"دوست\s+دارم\s+(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})",
            r"از\s+(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})\s+خوشم\s+می(?:اد|آید)",
            r"(?:ترجیح می‌?دم|ترجیح میدم)\s+(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})",
            r"\bi\s+(?:like|love|prefer)\s+(?P<thing>[a-zA-Z0-9\s-]{2,60})",
        ]
        dislikes = [
            r"از\s+(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})\s+بدم\s+می(?:اد|آید)",
            r"(?P<thing>[\w\u0600-\u06FF\s‌-]{2,80})\s+دوست\s+ندارم",
            r"\bi\s+(?:dislike|hate)\s+(?P<thing>[a-zA-Z0-9\s-]{2,60})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            thing = self._clean_memory_object(match.group("thing"))
            if not thing:
                continue
            return [
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary=f"User likes {thing}.",
                    confidence=0.86,
                    importance=3,
                )
            ]
        for pattern in dislikes:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            thing = self._clean_memory_object(match.group("thing"))
            if not thing:
                continue
            return [
                MemorySuggestion(
                    action="create",
                    kind="boundary",
                    summary=f"User dislikes {thing}.",
                    confidence=0.84,
                    importance=3,
                )
            ]
        return []

    def _clean_memory_object(self, value: str) -> str | None:
        thing = re.sub(r"\s+", " ", value or "").strip(" .،,!؟")
        lowered = thing.lower()
        if not thing or lowered in {"من", "i", "it"}:
            return None
        noisy_fragments = ["اسمم", "اسم من", " است و", " هستم و", "من "]
        if any(fragment in lowered for fragment in noisy_fragments):
            return None
        if len(thing.split()) > 8:
            return None
        return thing

    def _extract_relationship_style(self, text: str) -> list[MemorySuggestion]:
        lowered = text.lower()
        candidates: list[MemorySuggestion] = []
        style_triggers = {
            "شوخی": "User enjoys playful jokes in the conversation.",
            "سر به سر": "User enjoys light teasing when it stays friendly.",
            "کل کل": "User enjoys friendly banter.",
            "صمیمی": "User prefers a familiar and warm tone.",
            "لوس": "User likes a softer affectionate tone.",
        }
        for trigger, summary in style_triggers.items():
            if trigger in lowered:
                candidates.append(
                    MemorySuggestion(
                        action="create",
                        kind="inside_joke" if trigger in {"شوخی", "سر به سر", "کل کل"} else "preference",
                        summary=summary,
                        confidence=0.78,
                        importance=3,
                    )
                )
        if "اینجوری" in lowered and ("حرف بزن" in lowered or "جواب بده" in lowered):
            candidates.append(
                MemorySuggestion(
                    action="create",
                    kind="preference",
                    summary=self._summary("User preferred interaction style", text),
                    confidence=0.76,
                    importance=3,
                )
            )
        return candidates

    def _extract_stable_facts(self, text: str) -> list[MemorySuggestion]:
        patterns = [
            (r"(?:من|من\s+الان)?\s*(?:دانشجو|دانش‌آموز|برنامه‌نویس|توسعه‌دهنده|طراح|مدیر|پزشک|مهندس)\s*(?:هستم|ام)?", "identity"),
            (r"(?:کارم|شغلم)\s+(?P<value>[\w\u0600-\u06FF\s‌-]{2,80})\s*(?:است|ه)?", "identity"),
            (r"(?:روی|تو)\s+(?P<value>[\w\u0600-\u06FF\s‌-]{2,100})\s+کار\s+می(?:کنم|کنم)", "project"),
            (r"(?:دارم|میخوام|می‌خوام)\s+(?P<value>[\w\u0600-\u06FF\s‌-]{2,100})\s+(?:بسازم|درست کنم|راه بندازم)", "project"),
        ]
        for pattern, kind in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.groupdict().get("value")
            summary = self._summary("User stable fact", value or match.group(0))
            return [
                MemorySuggestion(
                    action="create",
                    kind=kind,
                    summary=summary,
                    confidence=0.76,
                    importance=4 if kind == "project" else 3,
                )
            ]
        return []

    def _extract_project_goal_constraint(self, text: str) -> list[MemorySuggestion]:
        lowered = text.lower()
        if "پروژه" in lowered or "project" in lowered:
            return [
                MemorySuggestion(
                    action="create",
                    kind="project",
                    summary=self._summary("User is working on", text),
                    confidence=0.75,
                    importance=4,
                )
            ]
        if "هدفم" in lowered or "goal" in lowered:
            return [
                MemorySuggestion(
                    action="create",
                    kind="goal",
                    summary=self._summary("User goal", text),
                    confidence=0.75,
                    importance=4,
                )
            ]
        if "نمی‌خوام" in lowered or "نمیخوام" in lowered or "do not" in lowered or "don't" in lowered:
            return [
                MemorySuggestion(
                    action="create",
                    kind="constraint",
                    summary=self._summary("User constraint", text),
                    confidence=0.75,
                    importance=4,
                )
            ]
        return []

    def _summary(self, prefix: str, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        return f"{prefix}: {compact[:190]}"

    def _dedupe_candidates(self, candidates: list[MemorySuggestion]) -> list[MemorySuggestion]:
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
    ) -> MemoryDecision:
        action = self.normalize_action(suggestion.action)
        summary = suggestion.summary.strip()
        lowered = f"{summary} {source_text}".lower()
        if assistant_sourced:
            return MemoryDecision(False, "assistant sourced memory is not allowed")
        if action == "create" and self.repository.active_count(user_id) >= self.max_active_memories:
            return MemoryDecision(False, "memory limit reached")
        if suggestion.confidence < 0.7:
            return MemoryDecision(False, "low confidence")
        if len(summary) < 10:
            return MemoryDecision(False, "too short")
        if any(word in lowered for word in SENSITIVE_WORDS):
            return MemoryDecision(False, "sensitive content")
        if any(word in lowered for word in INJECTION_WORDS):
            return MemoryDecision(False, "prompt injection content")
        if any(re.search(pattern, summary, re.IGNORECASE) for pattern in LOW_VALUE_PATTERNS):
            return MemoryDecision(False, "low value memory")
        if suggestion.kind == "temporary_event" or any(word in lowered for word in TEMPORARY_WORDS):
            return MemoryDecision(False, "temporary or mood-only content")
        if self._looks_like_raw_dialogue(summary):
            return MemoryDecision(False, "raw dialogue")
        if model_sourced and not self._source_or_existing_supports(source_text, suggestion, existing, action):
            return MemoryDecision(False, "not supported by current user message or active memory")
        return MemoryDecision(True, "accepted")

    def normalize_action(self, action: str) -> str:
        return "create" if action == "save" else action

    def _duplicate(self, existing: list[MemoryItem], suggestion: MemorySuggestion) -> bool:
        target_key = MemoryDeduplicator.canonical_key(suggestion.kind, suggestion.summary)
        for item in existing:
            if item.kind.value != suggestion.kind:
                continue
            if MemoryDeduplicator.canonical_key(item.kind.value, item.summary) == target_key:
                return True
            if MemoryDeduplicator.similarity(item.summary, suggestion.summary) >= 0.92:
                return True
        return False

    def _looks_like_raw_dialogue(self, summary: str) -> bool:
        return "\n" in summary or summary.count(":") >= 2 or len(summary.split()) > 34

    def _source_or_existing_supports(
        self,
        source_text: str,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
        action: str,
    ) -> bool:
        source = (source_text or "").lower()
        summary = (suggestion.summary or "").lower()
        if action in {"delete", "forget"}:
            forget_words = ("forget", "delete", "remove", "حذف", "فراموش", "یادت نماند", "دیگه یاد")
            return any(word in source for word in forget_words)
        if self._has_keyword_overlap(source, summary):
            return True
        if suggestion.kind == "identity" and any(word in source for word in ("اسم", "صدام", "صدا", "call me", "name")):
            return True
        if suggestion.kind == "inside_joke" and any(word in source for word in ("شوخی", "کل کل", "سر به سر", "joke", "banter", "tease")):
            return True
        if suggestion.kind == "project" and any(word in source for word in ("پروژه", "دارم می‌سازم", "دارم میسازم", "روی", "project", "working on")):
            return True
        if suggestion.kind in {"goal", "constraint", "unresolved_topic"} and any(
            word in source for word in ("هدف", "می‌خوام", "میخوام", "نباید", "نمی‌خوام", "نمیخوام", "goal", "want", "do not", "don't")
        ):
            return True
        if action in {"edit", "merge", "replace"}:
            target = self._find_existing_target(existing, suggestion)
            return target is not None and self._has_keyword_overlap(summary, target.summary.lower())
        return False

    def _find_existing_target(self, existing: list[MemoryItem], suggestion: MemorySuggestion) -> MemoryItem | None:
        canonical = MemoryDeduplicator.canonical_key(suggestion.kind, suggestion.summary)
        same_kind = [item for item in existing if item.kind.value == suggestion.kind]
        for item in same_kind:
            if MemoryDeduplicator.canonical_key(item.kind.value, item.summary) == canonical:
                return item
        scored = [(MemoryDeduplicator.similarity(item.summary, suggestion.summary), item) for item in same_kind]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] >= 0.88 else None

    def _has_keyword_overlap(self, left: str, right: str) -> bool:
        stop_words = {
            "user",
            "likes",
            "like",
            "prefers",
            "wants",
            "called",
            "fact",
            "the",
            "and",
            "with",
            "برای",
            "کاربر",
            "دوست",
            "دارد",
            "میخواهد",
            "می‌خواهد",
        }
        left_words = {word for word in re.findall(r"[\w\u0600-\u06FF‌]{3,}", left) if word not in stop_words}
        right_words = {word for word in re.findall(r"[\w\u0600-\u06FF‌]{3,}", right) if word not in stop_words}
        return bool(left_words & right_words)


class MemoryDeduplicator:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def find_target(self, existing: list[MemoryItem], suggestion: MemorySuggestion) -> MemoryItem | None:
        canonical = self.canonical_key(suggestion.kind, suggestion.summary)
        same_kind = [item for item in existing if item.kind.value == suggestion.kind]
        for item in same_kind:
            if self.canonical_key(item.kind.value, item.summary) == canonical:
                return item
        scored = [
            (self.similarity(item.summary, suggestion.summary), item)
            for item in same_kind
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if scored and scored[0][0] >= 0.88:
            return scored[0][1]
        return None

    @staticmethod
    def canonical_key(kind: str, summary: str) -> str:
        normalized = MemoryDeduplicator.normalize(summary)
        if kind == "identity" and "called" in normalized:
            return f"identity:name:{normalized.rsplit(' ', 1)[-1].strip('.')}"
        if kind == "preference":
            normalized = normalized.replace("user likes ", "").replace("user prefers ", "")
            return f"preference:{normalized.strip('.')}"
        return f"{kind}:{normalized}"

    @staticmethod
    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").lower()).strip()

    @staticmethod
    def similarity(left: str, right: str) -> float:
        return SequenceMatcher(None, MemoryDeduplicator.normalize(left), MemoryDeduplicator.normalize(right)).ratio()


class MemoryRetriever:
    def __init__(self, repository: MemoryRepository, max_context_tokens: int) -> None:
        self.repository = repository
        self.max_context_tokens = max_context_tokens

    def retrieve(self, user_id: int, text: str, limit: int = 10) -> list[MemoryItem]:
        memories = self.repository.list_active(user_id, limit=80)
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
            self.repository.touch(item.id)
        return selected

    def _keywords(self, text: str) -> set[str]:
        return {word.lower() for word in re.findall(r"[\w\u0600-\u06FF]{3,}", text or "")}


class MemoryService:
    def __init__(
        self,
        database: Database,
        max_active_memories: int = 80,
        max_context_tokens: int = 520,
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

    def retrieve_relevant(self, user_id: int, text: str, limit: int = 10) -> list[MemoryItem]:
        return self.retriever.retrieve(user_id, text, limit)

    def process_user_message(
        self,
        user_id: int,
        source_message_id: int | None,
        user_text: str,
        metadata: dict | None = None,
    ) -> None:
        existing = self.repository.list_active(user_id, limit=80)
        candidates = self.extractor.extract(user_text, existing, metadata)
        self.apply_candidates(user_id, source_message_id, user_text, candidates, assistant_sourced=False)

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
    ) -> None:
        for suggestion in suggestions:
            existing = self.repository.list_active(user_id, limit=80)
            action = self.policy.normalize_action(suggestion.action)
            decision = self.policy.decide(
                user_id,
                source_text,
                suggestion,
                existing,
                assistant_sourced=assistant_sourced,
                model_sourced=model_sourced,
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

    def upsert_identity_name(self, user_id: int, name: str, source_message_id: int | None = None) -> None:
        suggestion = MemorySuggestion(
            action="replace",
            kind="identity",
            summary=f"User wants to be called {name}.",
            confidence=1,
            importance=5,
        )
        self.apply_candidates(user_id, source_message_id, f"call me {name}", [suggestion], assistant_sourced=False)

    def delete(self, user_id: int, memory_id: int) -> bool:
        before = self.repository.get(user_id, memory_id)
        if before is None:
            return False
        deleted = self.repository.deactivate(memory_id)
        self.audit.log(user_id, memory_id, "delete", "accepted", "user requested deletion", before.model_dump(), None, None)
        return deleted is not None

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
            self.audit.log(user_id, target.id, "edit", "accepted", "updated duplicate memory", before, after.model_dump(mode="json") if after else None, source_message_id)
            return
        memory_id = self.repository.save(user_id, source_message_id, suggestion, "create")
        self.audit.log(user_id, memory_id, "create", "accepted", "stored", None, suggestion.model_dump(), source_message_id)
        self.audit.debug("memory_saved", user_id, {"memory_id": memory_id, "suggestion": suggestion.model_dump()})

    def _upsert_existing(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
        action: str,
    ) -> None:
        target = self.deduplicator.find_target(existing, suggestion)
        if target is None:
            memory_id = self.repository.save(user_id, source_message_id, suggestion, "create")
            self.audit.log(user_id, memory_id, "create", "accepted", f"{action} target not found; created", None, suggestion.model_dump(), source_message_id)
            return
        before = target.model_dump(mode="json")
        after = self.repository.update(target.id, suggestion)
        self.audit.log(user_id, target.id, action, "accepted", "updated matching memory", before, after.model_dump(mode="json") if after else None, source_message_id)

    def _delete_similar(
        self,
        user_id: int,
        source_message_id: int | None,
        suggestion: MemorySuggestion,
        existing: list[MemoryItem],
    ) -> None:
        target = self.deduplicator.find_target(existing, suggestion)
        if target is None:
            self.audit.log(user_id, None, "delete", "rejected", "matching memory not found", None, suggestion.model_dump(), source_message_id)
            return
        before = target.model_dump(mode="json")
        self.repository.deactivate(target.id)
        self.audit.log(user_id, target.id, "delete", "accepted", "deleted matching memory", before, None, source_message_id)
