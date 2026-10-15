"""Unit-specific direct/equipment expenses; only approved entries affect cost."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import BigInteger, String, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.modules.audit import service as audit
from app.modules.projects.models import ProductionUnit
from app.modules.manufacturing.settlements import APPROVE, PREPARE, READ, ApprovalInput, cents
from app.platform.database import Base, get_db
from app.platform.security import User, current_user, require_roles
from app.shared.errors import Conflict, DomainError, NotFound, PermissionDenied

router = APIRouter(prefix="/api/v1", tags=["Unit expenses and equipment"])


class UnitExpense(Base):
    __tablename__ = "unit_expenses"
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(index=True)
    source_reference: Mapped[str] = mapped_column(String(128), unique=True)
    category: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(512))
    incurred_date: Mapped[str] = mapped_column(String(10))
    net_cents: Mapped[int] = mapped_column(BigInteger)
    nonrecoverable_tax_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    created_by: Mapped[int] = mapped_column()
    approved_by: Mapped[int | None] = mapped_column(default=None)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    approved_at: Mapped[datetime | None] = mapped_column(default=None)


class ExpenseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    unit_id: int = Field(gt=0)
    source_reference: str = Field(min_length=1, max_length=128)
    category: Literal["DIRECT", "EQUIPMENT"]
    description: str = Field(min_length=3, max_length=512)
    incurred_date: date
    net_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    nonrecoverable_tax: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=2)
    currency: Literal["SAR"] = "SAR"


def serialize(record):
    return {"id": record.id, "unit_id": record.unit_id,
            "source_reference": record.source_reference, "category": record.category,
            "description": record.description, "incurred_date": record.incurred_date,
            "net_amount": record.net_cents / 100,
            "nonrecoverable_tax": record.nonrecoverable_tax_cents / 100,
            "total": (record.net_cents + record.nonrecoverable_tax_cents) / 100,
            "currency": "SAR", "status": record.status, "created_by": record.created_by,
            "approved_by": record.approved_by, "note": record.note}


def lock_unit(db, unit_id):
    db.execute(update(ProductionUnit).where(ProductionUnit.id == unit_id)
               .values(status=ProductionUnit.status))
    if db.get(ProductionUnit, unit_id) is None:
        raise NotFound("الوحدة غير موجودة")


@router.get("/unit-expenses", dependencies=[Depends(require_roles(*READ))])
def list_expenses(unit_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(UnitExpense).order_by(UnitExpense.id)
    if unit_id is not None:
        query = query.filter_by(unit_id=unit_id)
    return [serialize(e) for e in query.all()]


@router.post("/unit-expenses", status_code=201,
             dependencies=[Depends(require_roles(*PREPARE))])
def create_expense(body: ExpenseInput, db: Session = Depends(get_db),
                   actor: User = Depends(current_user)):
    from app.modules.manufacturing.financial_guards import ensure_unit_open
    ensure_unit_open(db, body.unit_id)
    ref = body.source_reference.upper()
    # An allocated freight document must not be entered a second time here.
    from app.modules.manufacturing.landed_costs import LandedCost
    if db.query(LandedCost).filter_by(source_reference=ref).first():
        raise Conflict("المرجع مسجل في الشحن والجمارك؛ تجنب التحميل المزدوج")
    if db.query(UnitExpense).filter_by(source_reference=ref).first():
        raise Conflict("مرجع المصروف مسجل سابقاً")
    rec = UnitExpense(unit_id=body.unit_id, source_reference=ref, category=body.category,
                      description=body.description, incurred_date=body.incurred_date.isoformat(),
                      net_cents=cents(body.net_amount),
                      nonrecoverable_tax_cents=cents(body.nonrecoverable_tax),
                      created_by=actor.id)
    db.add(rec)
    try:
        db.flush()
        audit.record(db, actor=actor.username, event="expense.created",
                     entity_type="unit_expense", entity_id=rec.id, after=serialize(rec))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("مرجع المصروف مستخدم في جلسة أخرى") from exc
    return serialize(rec)


@router.post("/unit-expenses/{expense_id}/approve",
             dependencies=[Depends(require_roles(*APPROVE))])
def approve_expense(expense_id: int, body: ApprovalInput, db: Session = Depends(get_db),
                    actor: User = Depends(current_user)):
    rec = db.get(UnitExpense, expense_id)
    if rec is None:
        raise NotFound("المصروف غير موجود")
    from app.modules.manufacturing.financial_guards import ensure_unit_open
    ensure_unit_open(db, rec.unit_id)
    db.refresh(rec)
    if rec.status != "DRAFT":
        raise Conflict("المصروف ليس مسودة قابلة للاعتماد")
    if rec.created_by == actor.id:
        raise PermissionDenied("لا يجوز لمعد المصروف اعتماده")
    if not body.note.strip():
        raise DomainError("ملاحظة الاعتماد مطلوبة")
    rec.status, rec.approved_by, rec.note = "APPROVED", actor.id, body.note.strip()
    rec.approved_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor.username, event="expense.approved",
                 entity_type="unit_expense", entity_id=rec.id, after=serialize(rec))
    db.commit()
    return serialize(rec)


@router.post("/unit-expenses/{expense_id}/cancel",
             dependencies=[Depends(require_roles(*PREPARE, *APPROVE))])
def cancel_expense(expense_id: int, body: ApprovalInput, db: Session = Depends(get_db),
                   actor: User = Depends(current_user)):
    rec = db.get(UnitExpense, expense_id)
    if rec is None:
        raise NotFound("المصروف غير موجود")
    from app.modules.manufacturing.financial_guards import ensure_unit_open
    ensure_unit_open(db, rec.unit_id)
    db.refresh(rec)
    if rec.status != "DRAFT":
        raise Conflict("لا يمكن إلغاء مصروف معتمد؛ يلزم قيد عكسي موثق")
    if actor.id != rec.created_by and actor.role not in APPROVE:
        raise PermissionDenied("إلغاء المسودة لمعدها أو مسؤول الاعتماد")
    if not body.note.strip():
        raise DomainError("سبب الإلغاء مطلوب")
    rec.status, rec.note = "CANCELLED", body.note.strip()
    audit.record(db, actor=actor.username, event="expense.cancelled",
                 entity_type="unit_expense", entity_id=rec.id, after=serialize(rec))
    db.commit()
    return serialize(rec)


def unit_expenses(db, unit_id):
    records = db.query(UnitExpense).filter_by(unit_id=unit_id, status="APPROVED").all()
    amounts = {key: sum(r.net_cents + r.nonrecoverable_tax_cents
                        for r in records if r.category == key) / 100
               for key in ("DIRECT", "EQUIPMENT")}
    return {**amounts, "total": round(sum(amounts.values()), 2),
            "sources": [serialize(r) for r in records]}
