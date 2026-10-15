"""Purchase orders and payment requests (separate lifecycles)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    material_request_id: Mapped[int] = mapped_column(index=True)
    sourcing_case_id: Mapped[int | None] = mapped_column(default=None)
    approval_id: Mapped[int] = mapped_column(index=True)      # binding approval
    supplier: Mapped[str] = mapped_column(String(128), index=True)
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    # lines: [{item_code, description, quantity, uom, unit_price}, ...]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    shipping_cost: Mapped[float] = mapped_column(default=0.0)
    extra_costs: Mapped[float] = mapped_column(default=0.0)
    non_refundable_tax: Mapped[float] = mapped_column(default=0.0)
    discount: Mapped[float] = mapped_column(default=0.0)
    delivery_days: Mapped[int] = mapped_column(default=0)
    payment_terms: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(24), default="ISSUED", index=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    def total(self) -> float:
        return round(
            sum(l["quantity"] * l["unit_price"] for l in self.lines)
            + self.shipping_cost + self.extra_costs + self.non_refundable_tax
            - self.discount, 2
        )


# Payment lifecycle (independent of the request lifecycle):
# DRAFT → PENDING_APPROVAL → APPROVED → PROCESSING → PAID → RECONCILED
# sub-states: FAILED | CANCELLED | PARTIALLY_PAID
class PaymentRequest(Base):
    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    pay_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    purchase_order_id: Mapped[int] = mapped_column(index=True)
    requested_by: Mapped[int] = mapped_column(index=True)
    approved_by: Mapped[int | None] = mapped_column(default=None)
    amount: Mapped[float] = mapped_column(default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    beneficiary: Mapped[str] = mapped_column(String(128), default="")
    method: Mapped[str] = mapped_column(String(32), default="")  # bank | custody | card
    purpose: Mapped[str] = mapped_column(String(255), default="")  # advance | final
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    bank_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    PAYMENT_TRANSITIONS: ClassVar = {
        "DRAFT": {"PENDING_APPROVAL", "CANCELLED"},
        "PENDING_APPROVAL": {"APPROVED", "CANCELLED"},
        "APPROVED": {"PROCESSING", "CANCELLED"},
        "PROCESSING": {"PAID", "FAILED", "PARTIALLY_PAID"},
        "PARTIALLY_PAID": {"PROCESSING", "PAID"},
        "PAID": {"RECONCILED"},
        "FAILED": {"PROCESSING", "CANCELLED"},
        "RECONCILED": set(),
        "CANCELLED": set(),
    }
