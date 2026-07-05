import tempfile
import unittest
from pathlib import Path

from bot.models.billing import InvoiceStatus
from bot.services.billing_service import BillingService
from bot.storage.database import Database


class BillingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.tmp.name) / "test.sqlite3"))
        self.database.migrate()
        self.service = BillingService(self.database)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_invoice_starts_pending_and_checkout_is_user_scoped(self) -> None:
        invoice = self.service.create_invoice(1, "stars_100")
        payload = self.service.payload_for_invoice(invoice)

        self.assertEqual(invoice.status, InvoiceStatus.PENDING)
        self.assertTrue(self.service.can_checkout(1, payload))
        self.assertFalse(self.service.can_checkout(2, payload))

    def test_successful_payment_marks_paid_once(self) -> None:
        invoice = self.service.create_invoice(1, "stars_200")
        payload = self.service.payload_for_invoice(invoice)

        first = self.service.confirm_successful_stars_payment(1, payload, "payment-1")
        second = self.service.confirm_successful_stars_payment(1, payload, "payment-1")

        self.assertTrue(first.accepted)
        self.assertTrue(first.newly_paid)
        self.assertEqual(first.invoice.message_quota, 200)
        self.assertTrue(second.accepted)
        self.assertFalse(second.newly_paid)

    def test_reused_payment_id_is_rejected_for_other_invoice(self) -> None:
        first_invoice = self.service.create_invoice(1, "stars_100")
        second_invoice = self.service.create_invoice(1, "stars_400")

        first = self.service.confirm_successful_stars_payment(
            1,
            self.service.payload_for_invoice(first_invoice),
            "payment-1",
        )
        second = self.service.confirm_successful_stars_payment(
            1,
            self.service.payload_for_invoice(second_invoice),
            "payment-1",
        )

        self.assertTrue(first.newly_paid)
        self.assertFalse(second.accepted)


if __name__ == "__main__":
    unittest.main()
