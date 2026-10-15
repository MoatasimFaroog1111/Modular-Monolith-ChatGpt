"""Approved, auditable freight/customs allocations; no automatic ERP posting."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, BigInteger, String, UniqueConstraint, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.modules.audit import service as audit
from app.modules.projects.models import ProductionUnit
from app.platform.database import Base, get_db
from app.platform.security import Role, require_roles
from app.shared.errors import Conflict, DomainError, NotFound, PermissionDenied


class LandedCost(Base):
    __tablename__ = "landed_costs"
    __table_args__ = (UniqueConstraint("source_reference", name="uq_landed_cost_reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_reference: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    # Integer minor units avoid floating point drift in financial allocation.
    freight_cents: Mapped[int] = mapped_column(BigInteger)
    customs_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    basis: Mapped[str] = mapped_column(String(16))
    lines: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", index=True)
    created_by: Mapped[int] = mapped_column(index=True)
    approved_by: Mapped[int | None] = mapped_column(default=None)
    approval_note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    approved_at: Mapped[datetime | None] = mapped_column(default=None)


class AllocationLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: int = Field(gt=0)
    # KG for WEIGHT; SAR for VALUE. EQUAL requires 1 for every container.
    basis_value: Decimal = Field(default=Decimal("1"), gt=0, max_digits=16, decimal_places=4)


class LandedCostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_reference: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    freight: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    customs: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    currency: Literal["SAR"] = "SAR"
    basis: Literal["EQUAL", "WEIGHT", "VALUE"]
    lines: list[AllocationLineIn] = Field(min_length=1, max_length=500)


class ApprovalIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    note: str = Field(min_length=3, max_length=512)


def _split(cents: int, weights: list[Decimal]) -> list[int]:
    """Largest remainder method; exact sum and deterministic ties by unit ID."""
    total = sum(weights)
    exact = [Decimal(cents) * w / total for w in weights]
    floor = [int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in exact]
    remainder = cents - sum(floor)
    priority = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floor[i]), i))
    for i in priority[:remainder]:
        floor[i] += 1
    return floor


def serialize(cost: LandedCost) -> dict:
    return {
        "id": cost.id, "source_reference": cost.source_reference,
        "description": cost.description, "currency": cost.currency,
        "basis": cost.basis, "freight": cost.freight_cents / 100,
        "customs": cost.customs_cents / 100,
        "total": (cost.freight_cents + cost.customs_cents) / 100,
        "status": cost.status, "created_by": cost.created_by,
        "approved_by": cost.approved_by, "approval_note": cost.approval_note,
        "created_at": cost.created_at.isoformat(),
        "approved_at": cost.approved_at.isoformat() if cost.approved_at else None,
        "lines": cost.lines,
    }


def create_allocation(db: Session, actor, body: LandedCostIn) -> LandedCost:
    if actor.role not in (Role.ACCOUNTANT, Role.FINANCE):
        raise PermissionDenied("إعداد توزيع التكلفة للمحاسب أو المدير المالي فقط")
    reference = body.source_reference.strip().upper()
    if not reference:
        raise DomainError("مرجع المستند مطلوب")
    if db.query(LandedCost).filter_by(source_reference=reference).first():
        raise Conflict("مرجع مستند الشحن/الجمارك مسجل سابقاً؛ لا يُحمّل مرتين")
    freight = int((body.freight * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    customs = int((body.customs * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if freight + customs <= 0:
        raise DomainError("إجمالي الشحن والجمارك يجب أن يكون أكبر من صفر")
    rows = sorted(body.lines, key=lambda line: line.unit_id)
    if len({r.unit_id for r in rows}) != len(rows):
        raise DomainError("لا يمكن تكرار الحاوية في التوزيع")
    from app.modules.manufacturing.financial_guards import ensure_unit_open
    for row in rows:
        ensure_unit_open(db, row.unit_id)
    from app.modules.manufacturing.expenses import UnitExpense
    if db.query(UnitExpense).filter_by(source_reference=reference).first():
        raise Conflict("المرجع مسجل في مصاريف الوحدة؛ تجنب التحميل المزدوج")
    weights = []
    units = []
    for row in rows:
        unit = db.get(ProductionUnit, row.unit_id)
        if unit is None:
            raise NotFound(f"الوحدة {row.unit_id} غير موجودة")
        if unit.build_method != "CONTAINER":
            raise DomainError("هذا التوزيع مخصص للحاويات وليس FrameCAD")
        if body.basis == "EQUAL" and row.basis_value != 1:
            raise DomainError("التوزيع المتساوي يتطلب أساس 1 لكل حاوية")
        units.append(unit)
        weights.append(row.basis_value)
    freights, duties = _split(freight, weights), _split(customs, weights)
    lines = [
        {
            "unit_id": row.unit_id, "house_no": unit.house_no,
            "basis_value": str(row.basis_value),
            "freight_cents": freights[i], "customs_cents": duties[i],
            "freight": freights[i] / 100, "customs": duties[i] / 100,
            "total": (freights[i] + duties[i]) / 100,
        }
        for i, (row, unit) in enumerate(zip(rows, units))
    ]
    cost = LandedCost(
        source_reference=reference, description=body.description.strip(),
        freight_cents=freight, customs_cents=customs, currency=body.currency,
        basis=body.basis, lines=lines, status="DRAFT", created_by=actor.id,
    )
    db.add(cost)
    try:
        db.flush()
        audit.record(
            db, actor=actor.username, event="landed_cost.created",
            entity_type="landed_cost", entity_id=cost.id,
            after={"source_reference": reference, "basis": cost.basis, "lines": lines},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("مرجع التوزيع مسجل سابقاً") from exc
    return cost


def approve_allocation(db: Session, actor, cost_id: int, note: str) -> LandedCost:
    if actor.role not in (Role.GM, Role.FINANCE):
        raise PermissionDenied("اعتماد توزيع التكلفة للمدير العام أو المالي فقط")
    cost = db.get(LandedCost, cost_id)
    if cost is None:
        raise NotFound("توزيع التكلفة غير موجود")
    from app.modules.manufacturing.financial_guards import ensure_unit_open
    for uid in sorted(l["unit_id"] for l in cost.lines):
        ensure_unit_open(db, uid)
    db.refresh(cost)
    if actor.id == cost.created_by:
        raise PermissionDenied("فصل الصلاحيات: مُعدّ التوزيع لا يعتمده بنفسه")
    if cost.status != "DRAFT":
        raise Conflict("التوزيع معتمد سابقاً؛ لا يمكن تحميله مرة أخرى")
    now = datetime.now(timezone.utc)
    changed = db.execute(
        update(LandedCost).where(LandedCost.id == cost_id, LandedCost.status == "DRAFT")
        .values(status="APPROVED", approved_by=actor.id, approved_at=now, approval_note=note)
    )
    if changed.rowcount != 1:
        db.rollback()
        raise Conflict("تم اعتماد التوزيع في جلسة أخرى")
    audit.record(
        db, actor=actor.username, event="landed_cost.approved",
        entity_type="landed_cost", entity_id=cost_id,
        before={"status": "DRAFT"},
        after={"status": "APPROVED", "note": note, "lines": cost.lines},
    )
    db.commit()
    db.refresh(cost)
    return cost


def unit_landed_cost(db: Session, unit_id: int) -> dict:
    rows = db.query(LandedCost).filter_by(status="APPROVED").all()
    freight = customs = 0
    sources = []
    for row in rows:
        for line in row.lines:
            if line["unit_id"] == unit_id:
                freight += line["freight_cents"]
                customs += line["customs_cents"]
                sources.append({"allocation_id": row.id, "source_reference": row.source_reference,
                                "basis": row.basis, **line})
    return {"freight": freight / 100, "customs": customs / 100,
            "total": (freight + customs) / 100, "currency": "SAR", "sources": sources}


router = APIRouter(prefix="/api/v1/landed-costs", tags=["landed-costs"])
read_access = require_roles(Role.GM, Role.FINANCE, Role.ACCOUNTANT, Role.AUDITOR, Role.ENGINEER)


@router.post("", status_code=201)
def create(body: LandedCostIn, db: Session = Depends(get_db),
           actor=Depends(require_roles(Role.ACCOUNTANT, Role.FINANCE))):
    return serialize(create_allocation(db, actor, body))


@router.get("")
def list_allocations(db: Session = Depends(get_db), actor=Depends(read_access)):
    return [serialize(row) for row in db.query(LandedCost).order_by(LandedCost.id.desc()).all()]


@router.post("/{cost_id}/approve")
def approve(cost_id: int, body: ApprovalIn, db: Session = Depends(get_db),
            actor=Depends(require_roles(Role.GM, Role.FINANCE))):
    return serialize(approve_allocation(db, actor, cost_id, body.note.strip()))
