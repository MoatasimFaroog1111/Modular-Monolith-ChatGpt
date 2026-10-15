"""Projects, production units, manufacturing orders and work orders."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    customer: Mapped[str] = mapped_column(String(128), default="")
    location: Mapped[str] = mapped_column(String(128), default="")
    budget: Mapped[float] = mapped_column(default=0.0)


class ProductionUnit(Base):
    """One house / unit. build_method: CONTAINER | FRAMECAD."""

    __tablename__ = "production_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    house_no: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[int | None] = mapped_column(default=None, index=True)
    build_method: Mapped[str] = mapped_column(String(16), default="CONTAINER")
    model: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="PLANNED")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class ManufacturingOrder(Base):
    __tablename__ = "manufacturing_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(index=True)
    version: Mapped[int] = mapped_column(default=1)
    quantity: Mapped[float] = mapped_column(default=1.0)
    plan: Mapped[dict | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    # DRAFT → CONFIRMED → IN_PRODUCTION → DONE

    MO_TRANSITIONS: ClassVar = {
        "DRAFT": {"CONFIRMED", "CANCELLED"},
        "CONFIRMED": {"IN_PRODUCTION", "CANCELLED"},
        "IN_PRODUCTION": {"DONE"},
        "DONE": set(),
        "CANCELLED": set(),
    }


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    mo_id: Mapped[int] = mapped_column(index=True)
    operation: Mapped[str] = mapped_column(String(128))   # e.g. تجميع الفريمات
    work_center_id: Mapped[int | None] = mapped_column(default=None)
    sequence_no: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    # PENDING → IN_PROGRESS → DONE (BLOCKED is derived from dependencies)
    depends_on_id: Mapped[int | None] = mapped_column(default=None)
    estimated_hours: Mapped[float] = mapped_column(default=0.0)
    percent_complete: Mapped[float] = mapped_column(default=0.0)
    actual_hours: Mapped[float] = mapped_column(default=0.0)

    WO_TRANSITIONS: ClassVar = {
        "PENDING": {"IN_PROGRESS"},
        "IN_PROGRESS": {"DONE"},
        "DONE": set(),
    }
