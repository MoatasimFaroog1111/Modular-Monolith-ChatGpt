"""Material Request — the spine of the whole cycle (MR-2026-000145)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class MaterialRequest(Base):
    __tablename__ = "material_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)

    # references
    project_id: Mapped[int | None] = mapped_column(index=True, default=None)
    production_unit_id: Mapped[int | None] = mapped_column(index=True, default=None)
    manufacturing_order_id: Mapped[int | None] = mapped_column(default=None)
    requester_id: Mapped[int] = mapped_column(index=True)          # user id

    # request fields (mandatory set enforced by service)
    need_by_date: Mapped[str] = mapped_column(String(10), default="")   # YYYY-MM-DD
    item_code: Mapped[str | None] = mapped_column(String(64), default=None)
    item_description: Mapped[str] = mapped_column(String(255), default="")
    unmapped_item: Mapped[bool] = mapped_column(default=False)    # code not found in catalog
    quantity: Mapped[float] = mapped_column(default=0.0)
    uom: Mapped[str] = mapped_column(String(16), default="")
    purpose: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")  # NORMAL | URGENT | STOPPING
    budget_code: Mapped[str] = mapped_column(String(64), default="")
    evidence: Mapped[dict | None] = mapped_column(JSON, default=None)    # {type, ref}

    status: Mapped[str] = mapped_column(String(32), index=True, default="DRAFT")
    raw_message: Mapped[str | None] = mapped_column(Text, default=None)  # original WhatsApp/text

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    MANDATORY_FIELDS = (
        "requester_id", "need_by_date", "item_description", "quantity", "uom",
    )

    def missing_fields(self) -> list[str]:
        return [f for f in self.MANDATORY_FIELDS if not getattr(self, f, None)]
