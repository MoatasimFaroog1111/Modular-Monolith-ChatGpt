"""Vendor invoices, three-way match results and exception cases."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base

# match result types (per blueprint)
MATCHED = "MATCHED"
QTY_EXCEPTION = "QTY_EXCEPTION"
PRICE_EXCEPTION = "PRICE_EXCEPTION"
TAX_EXCEPTION = "TAX_EXCEPTION"
MISSING_RECEIPT = "MISSING_RECEIPT"
DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
UNLINKED = "UNLINKED"

INVOICE_STATUSES = [
    "DRAFT", "EXTRACTED", "MATCHING", "EXCEPTION", "READY_TO_POST",
    "EXPORTED", "POSTED_TO_ERP", "PAID_OR_RECONCILED", "CANCELLED",
]


class VendorInvoice(Base):
    __tablename__ = "vendor_invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(64), index=True)
    supplier: Mapped[str] = mapped_column(String(128), index=True)
    invoice_date: Mapped[str] = mapped_column(String(10), default="")
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    # lines: [{item_code, description, quantity, uom, unit_price}]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    tax_amount: Mapped[float] = mapped_column(default=0.0)
    charges: Mapped[float] = mapped_column(default=0.0)
    total: Mapped[float] = mapped_column(default=0.0)

    purchase_order_id: Mapped[int | None] = mapped_column(default=None, index=True)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    match_result: Mapped[dict | None] = mapped_column(JSON, default=None)
    attachment_id: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        UniqueConstraint("supplier", "invoice_no", name="uq_supplier_invoice_no"),
    )


class ExceptionCase(Base):
    __tablename__ = "exception_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(index=True)
    match_type: Mapped[str] = mapped_column(String(32))     # one of the exception types
    difference: Mapped[float] = mapped_column(default=0.0)
    justification: Mapped[str] = mapped_column(String(512), default="")
    owner_id: Mapped[int] = mapped_column(index=True)
    required_action: Mapped[str] = mapped_column(String(128), default="")
    due_date: Mapped[str] = mapped_column(String(10), default="")
    decision: Mapped[str] = mapped_column(String(32), default="OPEN")
    decided_by: Mapped[int | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
