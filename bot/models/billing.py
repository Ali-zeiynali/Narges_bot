from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"


@dataclass(frozen=True)
class BillingPlan:
    id: str
    title: str
    message_quota: int
    stars_cost: int


@dataclass(frozen=True)
class InvoiceRecord:
    invoice_id: str
    user_id: int
    plan_id: str
    stars_cost: int
    message_quota: int
    status: InvoiceStatus
    created_at: datetime
    updated_at: datetime
    payment_id: str | None = None


@dataclass(frozen=True)
class PaymentConfirmation:
    invoice: InvoiceRecord | None
    accepted: bool
    newly_paid: bool
    reason: str
