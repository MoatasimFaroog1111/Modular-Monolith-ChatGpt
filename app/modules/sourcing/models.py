"""Sourcing cases and supplier quotations (versioned, evidence-bound)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class SourcingCase(Base):
    """One sourcing case can group homogeneous requests for comparison."""

    __tablename__ = "sourcing_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    material_request_ids: Mapped[list] = mapped_column(JSON, default=list)
    owner_id: Mapped[int] = mapped_column(index=True)          # procurement user
    invited_suppliers: Mapped[list] = mapped_column(JSON, default=list)
    due_date: Mapped[str] = mapped_column(String(10), default="")
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class Quotation(Base):
    __tablename__ = "quotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    sourcing_case_id: Mapped[int] = mapped_column(index=True)
    supplier: Mapped[str] = mapped_column(String(128), index=True)
    supplier_vat: Mapped[str] = mapped_column(String(32), default="")
    version: Mapped[int] = mapped_column(default=1)             # per supplier
    currency: Mapped[str] = mapped_column(String(3), default="SAR")

    # lines: [{item_code, description, quantity, uom, unit_price}, ...]
    lines: Mapped[list] = mapped_column(JSON, default=list)

    shipping_cost: Mapped[float] = mapped_column(default=0.0)
    extra_costs: Mapped[float] = mapped_column(default=0.0)
    non_refundable_tax: Mapped[float] = mapped_column(default=0.0)
    discount: Mapped[float] = mapped_column(default=0.0)

    delivery_days: Mapped[int] = mapped_column(default=0)
    payment_terms: Mapped[str] = mapped_column(String(128), default="")
    valid_until: Mapped[str] = mapped_column(String(10), default="")

    attachment_id: Mapped[int | None] = mapped_column(default=None)   # evidence doc
    status: Mapped[str] = mapped_column(String(16), default="RECEIVED")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    def materials_total(self) -> float:
        return round(sum(l["quantity"] * l["unit_price"] for l in self.lines), 2)

    def comparative_cost(self) -> float:
        """التكلفة المقارنة = مواد + شحن + ضرائب غير قابلة للاسترداد + تكاليف إضافية - خصومات"""
        return round(
            self.materials_total() + self.shipping_cost + self.non_refundable_tax
            + self.extra_costs - self.discount, 2
        )

    def snapshot_dict(self) -> dict:
        """Canonical content used for approval binding (hash source)."""
        return {
            "supplier": self.supplier,
            "supplier_vat": self.supplier_vat,
            "version": self.version,
            "currency": self.currency,
            "lines": self.lines,
            "shipping_cost": self.shipping_cost,
            "extra_costs": self.extra_costs,
            "non_refundable_tax": self.non_refundable_tax,
            "discount": self.discount,
            "delivery_days": self.delivery_days,
            "payment_terms": self.payment_terms,
        }
