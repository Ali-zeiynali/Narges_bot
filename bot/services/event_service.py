import json
from contextlib import closing
from datetime import UTC, date, datetime

from bot.models.ai import EventSuggestion
from bot.models.state import DailyEvent, GlobalState
from bot.services.scheduler import DailyScheduler
from bot.storage.database import Database


class EventService:
    def __init__(self, database: Database, scheduler: DailyScheduler) -> None:
        self.database = database
        self.scheduler = scheduler

    def get_events_for_day(self, event_date: date) -> list[DailyEvent]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload FROM daily_events
                WHERE event_date = ?
                ORDER BY start_at ASC
                """,
                (event_date.isoformat(),),
            ).fetchall()

        if rows:
            return [DailyEvent.model_validate(json.loads(row["payload"])) for row in rows]

        events = self.scheduler.create_daily_events(event_date)
        for event in events:
            self._save_event(event_date, event)
        return events

    def attach_active_events(self, state: GlobalState, now: datetime | None = None) -> GlobalState:
        now = now or datetime.now(UTC)
        events = self.get_events_for_day(now.date())
        active = [event for event in events if event.start_at <= now <= event.expires_at]
        if active:
            current = active[0]
            state.activity = current.activity
            state.location = current.location
        state.active_events = active[:3]
        return state

    def validate_suggestion(self, suggestion: EventSuggestion, state: GlobalState) -> bool:
        if len(state.active_events) >= 3:
            return False
        titles = {event.title for event in state.active_events}
        return suggestion.title not in titles

    def _save_event(self, event_date: date, event: DailyEvent) -> None:
        self.database.execute(
            """
            INSERT OR IGNORE INTO daily_events(id, event_date, payload, start_at, end_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event_date.isoformat(),
                event.model_dump_json(),
                event.start_at.isoformat(),
                event.end_at.isoformat(),
                event.expires_at.isoformat(),
            ),
        )
