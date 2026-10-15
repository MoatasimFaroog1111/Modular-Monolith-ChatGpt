"""Versioned financial close snapshots, never overwriting historical costs."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.modules.audit import service as audit
from app.modules.accounts_payable.models import VendorInvoice
from app.modules.manufacturing.expenses import UnitExpense, lock_unit
from app.modules.manufacturing.landed_costs import LandedCost
from app.modules.manufacturing.models import MaterialIssue
from app.modules.manufacturing.settlements import (
    APPROVE, READ, ApprovalInput, MaterialSettlement, lock_po,
)
from app.modules.projects.models import ManufacturingOrder, ProductionUnit, WorkOrder
from app.platform.database import Base, get_db
from app.platform.security import User, current_user, require_roles
from app.shared.errors import Conflict, DomainError, NotFound, PermissionDenied

router = APIRouter(prefix="/api/v1", tags=["Unit financial closing"])


class FinancialClose(Base):
    __tablename__ = "unit_financial_closes"
    __table_args__ = (UniqueConstraint("unit_id", "revision", name="uq_unit_close_revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(index=True)
    revision: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), default="CLOSED")
    snapshot: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    closed_by: Mapped[int] = mapped_column()
    note: Mapped[str] = mapped_column(String(512))
    closed_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    reopened_by: Mapped[int | None] = mapped_column(default=None)
    reopen_note: Mapped[str] = mapped_column(String(512), default="")
    reopened_at: Mapped[datetime | None] = mapped_column(default=None)


class CloseInput(ApprovalInput):
    expected_summary_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReopenInput(ApprovalInput):
    close_id: int = Field(gt=0)


def active_close(db, unit_id):
    return (db.query(FinancialClose).filter_by(unit_id=unit_id, status="CLOSED")
            .order_by(FinancialClose.revision.desc()).first())


def summary_hash(summary):
    return hashlib.sha256(json.dumps(summary, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def metadata(rec):
    return {"id": rec.id, "revision": rec.revision, "status": rec.status,
            "snapshot_hash": rec.snapshot_hash, "closed_by": rec.closed_by,
            "note": rec.note, "closed_at": rec.closed_at.isoformat(),
            "reopened_by": rec.reopened_by, "reopen_note": rec.reopen_note,
            "reopened_at": rec.reopened_at.isoformat() if rec.reopened_at else None}


def frozen_summary(db, unit_id):
    rec = active_close(db, unit_id)
    if rec is None:
        return None
    if summary_hash(rec.snapshot) != rec.snapshot_hash:
        raise Conflict("بصمة لقطة الإقفال غير مطابقة؛ يلزم فحص التدقيق")
    return {**copy.deepcopy(rec.snapshot), "financial_close": metadata(rec)}


def unit_po_ids(db, unit_id):
    return {line["purchase_order_id"]
            for issue in db.query(MaterialIssue).filter_by(unit_id=unit_id).all()
            for line in issue.lines}


def preflight(db, unit_id):
    from app.modules.manufacturing.costing import unit_cost_summary
    unit = db.get(ProductionUnit, unit_id)
    if unit is None:
        raise NotFound("الوحدة غير موجودة")
    current = active_close(db, unit_id)
    summary = unit_cost_summary(db, unit_id)
    blockers = []
    if current:
        blockers.append("الوحدة مقفلة مالياً بالفعل")
    if unit.status not in ("DONE", "DELIVERED"):
        blockers.append("يجب اكتمال الإنتاج قبل الإقفال المالي")
    mos = db.query(ManufacturingOrder).filter_by(unit_id=unit_id).all()
    if not mos or any(m.status != "DONE" for m in mos):
        blockers.append("أوامر التصنيع غير مكتملة")
    if any(db.query(WorkOrder).filter(WorkOrder.mo_id == m.id,
                                     WorkOrder.status != "DONE").first() for m in mos):
        blockers.append("يوجد أمر عمل غير مكتمل")
    for key, label in {
        "no_approved_bom": "قائمة المواد غير معتمدة",
        "labor_rate_missing": "توجد ساعات عمالة بلا تكلفة موثقة",
        "currency_conversion_required": "توجد تكلفة بعملة تحتاج تحويل",
        "materials_unsettled": "المواد المصروفة لم تسو بالكامل بفواتير معتمدة",
    }.items():
        if summary["flags"].get(key):
            blockers.append(label)
    if summary["estimated"]["materials"] > 0 and not unit_po_ids(db, unit_id):
        blockers.append("لا يوجد صرف مواد رغم وجود مواد مقدرة")
    if summary["actual"]["overhead_pct"] is None:
        blockers.append("يجب ضبط نسبة التكاليف غير المباشرة صراحة، ولو بصفر")
    if db.query(UnitExpense).filter_by(unit_id=unit_id, status="DRAFT").first():
        blockers.append("توجد مصاريف في انتظار الاعتماد أو الإلغاء")
    if any(any(l["unit_id"] == unit_id for l in cost.lines)
           for cost in db.query(LandedCost).filter_by(status="DRAFT").all()):
        blockers.append("يوجد توزيع شحن وجمارك غير معتمد")
    po_ids = unit_po_ids(db, unit_id)
    if po_ids:
        settled_ids = {s.invoice_id for s in db.query(MaterialSettlement).filter(
            MaterialSettlement.purchase_order_id.in_(po_ids),
            MaterialSettlement.status == "APPROVED",
        ).all()}
        if any(i.id not in settled_ids for i in db.query(VendorInvoice).filter(
            VendorInvoice.purchase_order_id.in_(po_ids),
            VendorInvoice.status != "CANCELLED",
        ).all()):
            blockers.append("توجد فواتير مصدر لم تعتمد تسويتها")
        if db.query(MaterialSettlement).filter(
            MaterialSettlement.purchase_order_id.in_(po_ids),
            MaterialSettlement.status == "DRAFT",
        ).first():
            blockers.append("توجد تسوية مواد غير معتمدة")
        if db.query(VendorInvoice).filter(
            VendorInvoice.purchase_order_id.in_(po_ids),
            VendorInvoice.status.notin_(["READY_TO_POST", "EXPORTED", "POSTED_TO_ERP",
                                         "PAID_OR_RECONCILED", "CANCELLED"]),
        ).first():
            blockers.append("توجد فواتير مصدر غير مطابقة أو استثناءات غير معتمدة")
    return {"can_close": not blockers, "blockers": blockers,
            "summary": summary, "summary_hash": summary_hash(summary),
            "current_close": metadata(current) if current else None}


@router.get("/production-units/{unit_id}/financial-close/check",
            dependencies=[Depends(require_roles(*READ))])
def check_close(unit_id: int, db: Session = Depends(get_db)):
    return preflight(db, unit_id)


@router.get("/production-units/{unit_id}/financial-close",
            dependencies=[Depends(require_roles(*READ))])
def history(unit_id: int, db: Session = Depends(get_db)):
    if db.get(ProductionUnit, unit_id) is None:
        raise NotFound("الوحدة غير موجودة")
    return [{**metadata(r), "snapshot": r.snapshot} for r in db.query(FinancialClose)
            .filter_by(unit_id=unit_id).order_by(FinancialClose.revision).all()]


@router.post("/production-units/{unit_id}/financial-close",
             dependencies=[Depends(require_roles(*APPROVE))])
def close_unit(unit_id: int, body: CloseInput, db: Session = Depends(get_db),
               actor: User = Depends(current_user)):
    # Every PO-affecting command locks PO before unit. Detect newly added sources
    # after claiming the unit rather than reversing the lock order.
    locked_pos = unit_po_ids(db, unit_id)
    for po in sorted(locked_pos):
        lock_po(db, po)
    lock_unit(db, unit_id)
    if unit_po_ids(db, unit_id) != locked_pos:
        raise Conflict("تغيرت مصادر المواد أثناء الإقفال؛ أعد الفحص")
    check = preflight(db, unit_id)
    if not check["can_close"]:
        raise Conflict("تعذر الإقفال: " + "؛ ".join(check["blockers"]))
    if check["summary_hash"] != body.expected_summary_hash:
        raise Conflict("تغيرت التكلفة منذ المراجعة؛ أعد الفحص قبل الإقفال")
    if not body.note.strip():
        raise DomainError("مذكرة الإقفال مطلوبة")
    latest = (db.query(FinancialClose).filter_by(unit_id=unit_id)
              .order_by(FinancialClose.revision.desc()).first())
    rec = FinancialClose(unit_id=unit_id, revision=latest.revision + 1 if latest else 1,
                         snapshot=check["summary"], snapshot_hash=check["summary_hash"],
                         closed_by=actor.id, note=body.note.strip())
    db.add(rec)
    db.flush()
    audit.record(db, actor=actor.username, event="unit.financial_closed",
                 entity_type="production_unit", entity_id=unit_id, after=metadata(rec))
    db.commit()
    return {**metadata(rec), "snapshot": rec.snapshot}


@router.post("/production-units/{unit_id}/financial-close/reopen",
             dependencies=[Depends(require_roles(*APPROVE))])
def reopen_unit(unit_id: int, body: ReopenInput, db: Session = Depends(get_db),
                actor: User = Depends(current_user)):
    lock_unit(db, unit_id)
    rec = active_close(db, unit_id)
    if rec is None or rec.id != body.close_id:
        raise Conflict("الإقفال المطلوب ليس الإقفال النشط الحالي")
    if rec.closed_by == actor.id:
        raise PermissionDenied("إعادة الفتح تتطلب مسؤولاً مخولاً غير منفذ الإقفال")
    if not body.note.strip():
        raise DomainError("سبب إعادة الفتح مطلوب")
    rec.status, rec.reopened_by, rec.reopen_note = "REOPENED", actor.id, body.note.strip()
    rec.reopened_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor.username, event="unit.financial_reopened",
                 entity_type="production_unit", entity_id=unit_id, after=metadata(rec))
    db.commit()
    return metadata(rec)
