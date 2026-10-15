"""Work-center administration with audited changes and optimistic concurrency."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.manufacturing.models import WorkCenter
from app.modules.projects.models import ManufacturingOrder, ProductionUnit, WorkOrder
from app.platform.database import get_db
from app.platform.security import Role, current_user, require_roles
from app.shared.errors import Conflict, DomainError, NotFound


class WorkCenterIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=64)
    hourly_rate: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class WorkCenterUpdate(WorkCenterIn):
    version: int = Field(ge=1)
    is_active: bool = True


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_center_id: int | None = Field(default=None, gt=0)
    estimated_hours: float = Field(ge=0, le=100000, allow_inf_nan=False)


def serialize(center):
    return {"id": center.id, "name": center.name, "hourly_rate": center.hourly_rate,
            "is_active": center.is_active, "version": center.version, "currency": "SAR",
            "rate_unit": "person_hour"}


router = APIRouter(prefix="/api/v1", tags=["work-centers"])
edit_access = require_roles(Role.ADMIN, Role.GM)


@router.get("/work-centers")
def list_centers(db: Session = Depends(get_db), actor=Depends(current_user)):
    return [serialize(c) for c in db.query(WorkCenter).order_by(WorkCenter.id).all()]


@router.post("/work-centers")
def create_center(body: WorkCenterIn, db: Session = Depends(get_db), actor=Depends(edit_access)):
    if db.query(WorkCenter).filter_by(name=body.name).first():
        raise Conflict("اسم مركز العمل مستخدم سابقاً")
    center = WorkCenter(name=body.name, hourly_rate=float(body.hourly_rate))
    db.add(center)
    try:
        db.flush()
        audit.record(db, actor=actor.username, event="work_center.created",
                     entity_type="work_center", entity_id=center.id, after=serialize(center))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("اسم مركز العمل مستخدم سابقاً") from exc
    return serialize(center)


@router.patch("/work-centers/{center_id}")
def edit_center(center_id: int, body: WorkCenterUpdate, db: Session = Depends(get_db),
                actor=Depends(edit_access)):
    center = db.get(WorkCenter, center_id)
    if center is None:
        raise NotFound("مركز العمل غير موجود")
    before = serialize(center)
    if not body.is_active:
        in_use = db.query(WorkOrder).filter(
            WorkOrder.work_center_id == center_id, WorkOrder.status == "IN_PROGRESS"
        ).first()
        if in_use:
            raise Conflict("لا يمكن تعطيل مركز مرتبط بأمر عمل قيد التنفيذ")
    try:
        changed = db.execute(
            update(WorkCenter)
            .where(WorkCenter.id == center_id, WorkCenter.version == body.version)
            .values(name=body.name, hourly_rate=float(body.hourly_rate),
                    is_active=body.is_active, version=body.version + 1)
        )
        if changed.rowcount != 1:
            db.rollback()
            raise Conflict("تم تعديل المركز في جلسة أخرى؛ حدّث القائمة أولاً")
        db.refresh(center)
        audit.record(db, actor=actor.username, event="work_center.updated",
                     entity_type="work_center", entity_id=center_id,
                     before=before, after=serialize(center))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise Conflict("اسم مركز العمل مستخدم سابقاً") from exc
    return serialize(center)


@router.get("/production-units")
def list_units(db: Session = Depends(get_db), actor=Depends(current_user)):
    return [{"id": u.id, "house_no": u.house_no, "build_method": u.build_method,
             "status": u.status, "project_id": u.project_id}
            for u in db.query(ProductionUnit).order_by(ProductionUnit.id).all()]


@router.get("/production-units/{unit_id}/work-orders")
def list_work_orders(unit_id: int, db: Session = Depends(get_db), actor=Depends(current_user)):
    if db.get(ProductionUnit, unit_id) is None:
        raise NotFound("الوحدة غير موجودة")
    rows = (db.query(WorkOrder).join(ManufacturingOrder, WorkOrder.mo_id == ManufacturingOrder.id)
            .filter(ManufacturingOrder.unit_id == unit_id)
            .order_by(WorkOrder.mo_id, WorkOrder.sequence_no).all())
    return [{"id": wo.id, "operation": wo.operation, "status": wo.status,
             "manufacturing_order_id": wo.mo_id, "work_center_id": wo.work_center_id,
             "estimated_hours": wo.estimated_hours, "actual_hours": wo.actual_hours}
            for wo in rows]


@router.patch("/work-orders/{wo_id}/plan")
def plan_wo(wo_id: int, body: PlanIn, db: Session = Depends(get_db),
            actor=Depends(require_roles(Role.ENGINEER, Role.ADMIN))):
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise NotFound("أمر العمل غير موجود")
    from app.modules.manufacturing.financial_guards import ensure_unit_open
    mo = db.get(ManufacturingOrder, wo.mo_id)
    ensure_unit_open(db, mo.unit_id)
    db.refresh(wo)
    if wo.status != "PENDING":
        raise Conflict("خطة أمر العمل مقفلة بعد بدء التنفيذ")
    if body.work_center_id is not None:
        center = db.get(WorkCenter, body.work_center_id)
        if center is None or not center.is_active:
            raise DomainError("مركز العمل غير موجود أو معطل")
    before = {"work_center_id": wo.work_center_id, "estimated_hours": wo.estimated_hours}
    changed = db.execute(
        update(WorkOrder).where(WorkOrder.id == wo_id, WorkOrder.status == "PENDING")
        .values(work_center_id=body.work_center_id, estimated_hours=body.estimated_hours)
    )
    if changed.rowcount != 1:
        db.rollback()
        raise Conflict("بدأ تنفيذ أمر العمل في جلسة أخرى")
    audit.record(db, actor=actor.username, event="work_order.planned",
                 entity_type="work_order", entity_id=wo_id, before=before,
                 after=body.model_dump())
    db.commit()
    return {"id": wo.id, "work_center_id": body.work_center_id,
            "estimated_hours": body.estimated_hours}
