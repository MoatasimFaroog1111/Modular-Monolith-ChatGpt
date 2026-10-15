"""Goods receipts (GRN) and quality inspections."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    grn_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    purchase_order_id: Mapped[int] = mapped_column(index=True)
    supplier: Mapped[str] = mapped_column(String(128), default="")
    site: Mapped[str] = mapped_column(String(128), default="")
    received_by: Mapped[int] = mapped_column(index=True)
    # lines: [{item_code, description, qty_required, qty_supplied,
    #          qty_accepted, qty_rejected, condition, batch_no, notes}]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    photos: Mapped[list] = mapped_column(JSON, default=list)
    inspection_result: Mapped[str] = mapped_column(String(24), default="PENDING")
    # PENDING | ACCEPTED | ACCEPTED_WITH_RESERVE | REJECTED | QC_HOLD
    received_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class QualityInspection(Base):
    __tablename__ = "quality_inspections"

    id: Mapped[int] = mapped_column(primary_key=True)
    grn_id: Mapped[int | None] = mapped_column(default=None, index=True)
    production_unit_id: Mapped[int | None] = mapped_column(default=None)
    # points: [{point, expected, observed, pass}]
    points: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[str] = mapped_column(String(24), default="PENDING")
    inspector_id: Mapped[int] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
