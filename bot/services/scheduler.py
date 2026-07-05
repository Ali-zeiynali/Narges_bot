import random
from datetime import UTC, date, datetime, time, timedelta

from bot.models.ai import EventSuggestion
from bot.models.state import DailyEvent, GlobalState


class DailyScheduler:
    def create_daily_events(self, today: date | None = None) -> list[DailyEvent]:
        today = today or datetime.now(UTC).date()
        rng = random.Random(today.isoformat())
        count = rng.randint(1, 3)
        candidates = [
            ("مرور پیام‌ها", "checking messages", "home", time(10, 0), 45),
            ("کار روی یادداشت‌ها", "writing notes", "desk", time(13, 30), 60),
            ("استراحت کوتاه", "resting", "home", time(17, 15), 30),
            ("پیاده‌روی کوتاه", "walking", "outside", time(19, 0), 45),
        ]
        rng.shuffle(candidates)
        events = []
        for index, (title, activity, location, start_time, minutes) in enumerate(
            sorted(candidates[:count], key=lambda item: item[3])
        ):
            start_at = datetime.combine(today, start_time, UTC)
            end_at = start_at + timedelta(minutes=minutes)
            events.append(
                DailyEvent(
                    id=f"{today.isoformat()}-{index + 1}",
                    title=title,
                    activity=activity,
                    location=location,
                    topic=None,
                    start_at=start_at,
                    end_at=end_at,
                    expires_at=end_at + timedelta(hours=3),
                )
            )
        return events

    def attach_active_events(self, state: GlobalState, now: datetime | None = None) -> GlobalState:
        now = now or datetime.now(UTC)
        events = self.create_daily_events(now.date())
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
