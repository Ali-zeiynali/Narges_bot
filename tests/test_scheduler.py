import unittest
from datetime import date

from bot.services.scheduler import DailyScheduler


class SchedulerTests(unittest.TestCase):
    def test_daily_events_are_limited_and_ordered(self) -> None:
        events = DailyScheduler().create_daily_events(date(2026, 7, 5))

        self.assertGreaterEqual(len(events), 1)
        self.assertLessEqual(len(events), 3)
        for previous, current in zip(events, events[1:]):
            self.assertLessEqual(previous.end_at, current.start_at)


if __name__ == "__main__":
    unittest.main()
