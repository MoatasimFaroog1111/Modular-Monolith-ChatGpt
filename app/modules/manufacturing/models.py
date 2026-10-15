"""Manufacturing: BoM versions, work centers, progress events, material issues.

Phase 3 of the blueprint:
- BoM per production unit with versions and estimated costs.
- Manufacturing orders generated from stage templates per build method
  (CONTAINER vs FRAMECAD), with work orders chained by dependencies.
- Daily progress events: operation, percent, workers, hours, stoppages with
  reasons, and material-shortage flags that spawn linked material requests.
- Material issues to the unit costed from the PO price actually paid.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base

# Stage templates per build method (from the blueprint)
FRAMECAD_STAGES = [
    "التصميم المعتمد", "تجهيز ملف الماكينة", "تشكيل القطاعات", "فرز وترقيم",
    "تجميع الفريمات", "التغليف الخارجي", "العزل", "MEP", "الإغلاق الداخلي",
    "التشطيب", "الجودة والتسليم",
]
CONTAINER_STAGES = [
    "استلام الحاوية", "فحص", "قص وفتحات", "تدعيم", "معالجة وحماية", "عزل",
    "MEP", "تغليف", "تشطيب", "جودة وتسليم",
]
STAGE_TEMPLATES = {"FRAMECAD": FRAMECAD_STAGES, "CONTAINER": CONTAINER_STAGES}


class BoM(Base):
    """Bill of materials for a production unit, versioned and approved."""

    __tablename__ = "boms"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT | APPROVED
    created_by: Mapped[int] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class BoMLine(Base):
    __tablename__ = "bom_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    bom_id: Mapped[int] = mapped_column(index=True)
    item_code: Mapped[str | None] = mapped_column(default=None)
    description: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[float] = mapped_column(default=0.0)
    uom: Mapped[str] = mapped_column(String(16), default="")
    estimated_unit_price: Mapped[float] = mapped_column(default=0.0)


class WorkCenter(Base):
    __tablename__ = "work_centers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    hourly_rate: Mapped[float] = mapped_column(default=0.0)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="1")
    version: Mapped[int] = mapped_column(default=1, server_default="1")


class ProgressEvent(Base):
    """Daily progress report for one unit / work order.

    Captured from a free message like: «H-07 خلصنا تجميع الجدارين، اشتغل 4
    فنيين 7 ساعات، وتوقفنا ساعتين لنقص البراغي» — structured, never invented.
    """

    __tablename__ = "progress_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(index=True)
    work_order_id: Mapped[int | None] = mapped_column(default=None, index=True)
    operation: Mapped[str] = mapped_column(String(128), default="")
    percent_complete: Mapped[float] = mapped_column(default=0.0)   # 0-100
    workers: Mapped[int] = mapped_column(default=0)
    hours: Mapped[float] = mapped_column(default=0.0)
    stoppages: Mapped[list] = mapped_column(JSON, default=list)    # [{hours, reason}]
    material_shortage: Mapped[bool] = mapped_column(default=False)
    shortage_description: Mapped[str | None] = mapped_column(default=None)
    linked_material_request_id: Mapped[int | None] = mapped_column(default=None)
    reported_by: Mapped[int] = mapped_column(index=True)
    labor_rate_used: Mapped[float] = mapped_column(default=0.0)
    labor_cost: Mapped[float] = mapped_column(default=0.0)
    event_date: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class MaterialIssue(Base):
    """صرف المواد للوحدة — costed at the PO price actually paid."""

    __tablename__ = "material_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    unit_id: Mapped[int] = mapped_column(index=True)
    issued_by: Mapped[int] = mapped_column(index=True)
    # lines: [{item_code, description, quantity, uom,
    #          purchase_order_id, unit_price}]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    def materials_cost(self) -> float:
        return round(sum(l["quantity"] * l["unit_price"] for l in self.lines), 2)
