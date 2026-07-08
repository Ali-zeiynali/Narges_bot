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

    def test_card_invoice_receipt_and_review_flow(self) -> None:
        invoice = self.service.create_card_invoice(1, "card_100")

        self.assertEqual(invoice.status, InvoiceStatus.PENDING)
        self.assertEqual(invoice.payment_method, "card")
        self.assertEqual(invoice.toman_cost, 150_000)

        with_receipt = self.service.attach_card_receipt(1, "tracking-123")
        self.assertIsNotNone(with_receipt)
        self.assertEqual(with_receipt.payment_id, "tracking-123")

        approved = self.service.review_card_invoice(invoice.invoice_id, approve=True, reviewer_id=99)
        second = self.service.review_card_invoice(invoice.invoice_id, approve=True, reviewer_id=99)

        self.assertTrue(approved.accepted)
        self.assertTrue(approved.newly_paid)
        self.assertEqual(approved.invoice.status, InvoiceStatus.PAID)
        self.assertTrue(second.accepted)
        self.assertFalse(second.newly_paid)

    def test_card_invoice_can_be_rejected(self) -> None:
        invoice = self.service.create_card_invoice(1, "card_200")

        rejected = self.service.review_card_invoice(invoice.invoice_id, approve=False, reviewer_id=99)

        self.assertTrue(rejected.accepted)
        self.assertFalse(rejected.newly_paid)
        self.assertEqual(rejected.invoice.status, InvoiceStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
