import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from bot.services.event_service import EventService
from bot.services.scheduler import DailyScheduler
from bot.storage.database import Database


class EventServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = EventService(self.database, DailyScheduler())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_daily_events_are_persisted(self) -> None:
        today = date(2026, 7, 5)
        first = self.service.get_events_for_day(today)
        second = self.service.get_events_for_day(today)

        self.assertEqual([event.id for event in first], [event.id for event in second])
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM daily_events").fetchone()
        self.assertEqual(row["count"], len(first))


if __name__ == "__main__":
    unittest.main()
