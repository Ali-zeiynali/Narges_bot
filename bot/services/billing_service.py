from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from bot.models.billing import BillingPlan, InvoiceRecord, InvoiceStatus, PaymentConfirmation
from bot.storage.database import Database
from bot.storage.orm import BillingInvoiceORM


STAR_PLANS: tuple[BillingPlan, ...] = (
    BillingPlan(id="stars_100", title="100 پیام", message_quota=100, stars_cost=100),
    BillingPlan(id="stars_200", title="200 پیام", message_quota=200, stars_cost=200),
    BillingPlan(id="stars_400", title="400 پیام", message_quota=400, stars_cost=400),
    BillingPlan(id="stars_600", title="600 پیام", message_quota=600, stars_cost=600),
    BillingPlan(id="stars_1000_discount", title="1000 پیام", message_quota=1000, stars_cost=800),
)

CARD_PLANS: tuple[BillingPlan, ...] = (
    BillingPlan(id="card_100", title="100 پیام", message_quota=100, stars_cost=0, toman_cost=150_000, payment_method="card"),
    BillingPlan(id="card_200", title="200 پیام", message_quota=200, stars_cost=0, toman_cost=300_000, payment_method="card"),
    BillingPlan(id="card_400", title="400 پیام", message_quota=400, stars_cost=0, toman_cost=600_000, payment_method="card"),
    BillingPlan(id="card_600", title="600 پیام", message_quota=600, stars_cost=0, toman_cost=900_000, payment_method="card"),
    BillingPlan(id="card_1000_discount", title="1000 پیام تخفیفی", message_quota=1000, stars_cost=0, toman_cost=1_200_000, payment_method="card"),
)


class BillingService:
    """Internal billing boundary, ready to be replaced by an API-backed backend."""

    PAYLOAD_PREFIX = "narges-stars"

    def __init__(self, database: Database) -> None:
        self.database = database
        self.plans = {plan.id: plan for plan in (*STAR_PLANS, *CARD_PLANS)}

    def list_star_plans(self) -> tuple[BillingPlan, ...]:
        return STAR_PLANS

    def list_card_plans(self) -> tuple[BillingPlan, ...]:
        return CARD_PLANS

    def get_plan(self, plan_id: str) -> BillingPlan | None:
        return self.plans.get(plan_id)

    def create_invoice(self, user_id: int, plan_id: str) -> InvoiceRecord:
        plan = self.plans[plan_id]
        now = datetime.now(UTC)
        invoice_id = uuid.uuid4().hex
        with self.database.orm.session() as session:
            row = BillingInvoiceORM(
                invoice_id=invoice_id,
                user_id=user_id,
                plan_id=plan.id,
                stars_cost=plan.stars_cost,
                message_quota=plan.message_quota,
                status=InvoiceStatus.PENDING.value,
                payment_id=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._row_to_invoice(row)

    def create_card_invoice(self, user_id: int, plan_id: str) -> InvoiceRecord:
        plan = self.plans[plan_id]
        if plan.payment_method != "card":
            raise ValueError("plan is not a card plan")
        return self.create_invoice(user_id, plan_id)

    def payload_for_invoice(self, invoice: InvoiceRecord) -> str:
        return f"{self.PAYLOAD_PREFIX}:{invoice.invoice_id}"

    def invoice_from_payload(self, payload: str) -> InvoiceRecord | None:
        invoice_id = self._invoice_id_from_payload(payload)
        if not invoice_id:
            return None
        return self.get_invoice(invoice_id)

    def get_invoice(self, invoice_id: str) -> InvoiceRecord | None:
        with self.database.orm.session() as session:
            row = session.get(BillingInvoiceORM, invoice_id)
            return self._row_to_invoice(row) if row else None

    def list_user_invoices(self, user_id: int, limit: int = 20) -> list[InvoiceRecord]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(BillingInvoiceORM)
                .where(BillingInvoiceORM.user_id == user_id)
                .order_by(BillingInvoiceORM.created_at.desc())
                .limit(limit)
            ).all()
            return [self._row_to_invoice(row) for row in rows]

    def latest_pending_card_invoice(self, user_id: int) -> InvoiceRecord | None:
        with self.database.orm.session() as session:
            row = session.scalar(
                select(BillingInvoiceORM)
                .where(
                    BillingInvoiceORM.user_id == user_id,
                    BillingInvoiceORM.status == InvoiceStatus.PENDING.value,
                    BillingInvoiceORM.plan_id.like("card_%"),
                )
                .order_by(BillingInvoiceORM.created_at.desc())
            )
            return self._row_to_invoice(row) if row else None

    def attach_card_receipt(self, user_id: int, receipt_reference: str) -> InvoiceRecord | None:
        receipt_reference = receipt_reference.strip()[:120]
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.scalar(
                select(BillingInvoiceORM)
                .where(
                    BillingInvoiceORM.user_id == user_id,
                    BillingInvoiceORM.status == InvoiceStatus.PENDING.value,
                    BillingInvoiceORM.plan_id.like("card_%"),
                )
                .order_by(BillingInvoiceORM.created_at.desc())
            )
            if row is None:
                return None
            row.payment_id = receipt_reference or f"manual:{row.invoice_id}"
            row.updated_at = now
            session.flush()
            return self._row_to_invoice(row)

    def list_reviewable_card_invoices(self, limit: int = 300) -> list[InvoiceRecord]:
        with self.database.orm.session() as session:
            rows = session.scalars(
                select(BillingInvoiceORM)
                .where(BillingInvoiceORM.plan_id.like("card_%"))
                .order_by(BillingInvoiceORM.status.asc(), BillingInvoiceORM.updated_at.desc())
                .limit(limit)
            ).all()
            return [self._row_to_invoice(row) for row in rows]

    def can_checkout(self, user_id: int, payload: str) -> bool:
        invoice = self.invoice_from_payload(payload)
        return bool(invoice and invoice.user_id == user_id and invoice.status == InvoiceStatus.PENDING)

    def confirm_successful_stars_payment(
        self,
        user_id: int,
        payload: str,
        payment_id: str,
    ) -> PaymentConfirmation:
        invoice_id = self._invoice_id_from_payload(payload)
        if not invoice_id:
            return PaymentConfirmation(None, False, False, "invalid payload")
        if not payment_id:
            return PaymentConfirmation(None, False, False, "missing payment id")

        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            reused_payment = session.scalar(
                select(BillingInvoiceORM).where(
                    BillingInvoiceORM.payment_id == payment_id,
                    BillingInvoiceORM.invoice_id != invoice_id,
                )
            )
            if reused_payment is not None:
                return PaymentConfirmation(self._row_to_invoice(reused_payment), False, False, "payment id already used")

            row = session.get(BillingInvoiceORM, invoice_id)
            if row is None:
                return PaymentConfirmation(None, False, False, "invoice not found")
            if row.user_id != user_id:
                return PaymentConfirmation(self._row_to_invoice(row), False, False, "user mismatch")
            if row.status == InvoiceStatus.PAID.value:
                return PaymentConfirmation(self._row_to_invoice(row), True, False, "already paid")
            if row.status != InvoiceStatus.PENDING.value:
                return PaymentConfirmation(self._row_to_invoice(row), False, False, f"invalid status {row.status}")

            row.status = InvoiceStatus.PAID.value
            row.payment_id = payment_id
            row.updated_at = now
            session.flush()
            return PaymentConfirmation(self._row_to_invoice(row), True, True, "paid")

    def review_card_invoice(self, invoice_id: str, *, approve: bool, reviewer_id: int | None = None) -> PaymentConfirmation:
        now = datetime.now(UTC)
        with self.database.orm.session() as session:
            row = session.get(BillingInvoiceORM, invoice_id)
            if row is None:
                return PaymentConfirmation(None, False, False, "invoice not found")
            if not row.plan_id.startswith("card_"):
                return PaymentConfirmation(self._row_to_invoice(row), False, False, "not a card invoice")
            if row.status == InvoiceStatus.PAID.value:
                return PaymentConfirmation(self._row_to_invoice(row), True, False, "already paid")
            if row.status == InvoiceStatus.FAILED.value:
                return PaymentConfirmation(self._row_to_invoice(row), True, False, "already rejected")

            row.status = InvoiceStatus.PAID.value if approve else InvoiceStatus.FAILED.value
            if not row.payment_id:
                row.payment_id = f"manual-review:{reviewer_id or 0}:{now.timestamp():.0f}"
            row.updated_at = now
            session.flush()
            return PaymentConfirmation(self._row_to_invoice(row), True, approve, "approved" if approve else "rejected")

    def mark_failed(self, invoice_id: str, reason: str = "failed") -> None:
        with self.database.orm.session() as session:
            row = session.get(BillingInvoiceORM, invoice_id)
            if row and row.status == InvoiceStatus.PENDING.value:
                row.status = InvoiceStatus.FAILED.value
                row.updated_at = datetime.now(UTC)

    def _invoice_id_from_payload(self, payload: str) -> str | None:
        prefix = f"{self.PAYLOAD_PREFIX}:"
        if not payload.startswith(prefix):
            return None
        invoice_id = payload.removeprefix(prefix).strip()
        return invoice_id if len(invoice_id) == 32 else None

    def _row_to_invoice(self, row: BillingInvoiceORM) -> InvoiceRecord:
        plan = self.get_plan(row.plan_id)
        return InvoiceRecord(
            invoice_id=row.invoice_id,
            user_id=row.user_id,
            plan_id=row.plan_id,
            stars_cost=row.stars_cost,
            message_quota=row.message_quota,
            status=InvoiceStatus(row.status),
            payment_id=row.payment_id,
            created_at=self._dt(row.created_at),
            updated_at=self._dt(row.updated_at),
            toman_cost=plan.toman_cost if plan else 0,
            payment_method=plan.payment_method if plan else ("card" if row.plan_id.startswith("card_") else "stars"),
        )

    def _dt(self, value: datetime | str) -> datetime:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
