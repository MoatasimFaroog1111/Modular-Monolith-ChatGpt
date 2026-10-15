"""Manufacturing service: BoM, MO/WO lifecycle, progress, material issues."""
from __future__ import annotations

from datetime import datetime, timezone
import math

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.manufacturing import models as mfg
from app.modules.material_requests import service as mr_service
from app.modules.projects.models import ManufacturingOrder, ProductionUnit, WorkOrder
from app.modules.receiving import service as receiving
from app.modules.purchasing.models import PurchaseOrder
from app.platform.security import Role, User
from app.shared.errors import Conflict, DomainError, NotFound, PermissionDenied
from app.modules.manufacturing.financial_guards import ensure_unit_open
from app.shared.ids import next_number

# ---------------------------------------------------------------------------
# Production units
# ---------------------------------------------------------------------------

def create_unit(db: Session, *, actor: User, payload: dict) -> ProductionUnit:
    if actor.role not in (Role.ENGINEER, Role.ADMIN):
        raise PermissionDenied("إنشاء وحدات الإنتاج للمهندسين فقط")
    build_method = payload.get("build_method", "CONTAINER").upper()
    if build_method not in mfg.STAGE_TEMPLATES:
        raise DomainError("build_method يجب أن يكون CONTAINER أو FRAMECAD")
    unit = ProductionUnit(
        house_no=payload.get("house_no", ""),
        project_id=payload.get("project_id"),
        build_method=build_method,
        model=payload.get("model", ""),
    )
    db.add(unit)
    db.flush()
    audit.record(db, actor=actor.username, event="unit.created",
                 entity_type="production_unit", entity_id=unit.id,
                 after={"house_no": unit.house_no, "build_method": build_method})
    db.commit()
    return unit


def get_unit_or_404(db: Session, unit_id: int) -> ProductionUnit:
    unit = db.get(ProductionUnit, unit_id)
    if unit is None:
        raise NotFound(f"وحدة الإنتاج {unit_id} غير موجودة")
    return unit


# ---------------------------------------------------------------------------
# BoM
# ---------------------------------------------------------------------------

def create_bom(
    db: Session, *, actor: User, unit_id: int, lines: list[dict],
    approve: bool = False,
) -> mfg.BoM:
    if actor.role not in (Role.ENGINEER, Role.PROCUREMENT, Role.ADMIN):
        raise PermissionDenied("إنشاء BoM للمهندسين/المشتريات فقط")
    ensure_unit_open(db, unit_id)
    if not lines:
        raise DomainError("قائمة المواد يجب أن تحتوي بنوداً")
    for ln in lines:
        quantity = float(ln.get("quantity", 0))
        price = float(ln.get("estimated_unit_price", 0))
        if not math.isfinite(quantity) or quantity <= 0:
            raise DomainError("كمية غير صالحة في بنود BoM")
        if not math.isfinite(price) or price < 0:
            raise DomainError("سعر BoM يجب أن يكون رقماً غير سالب")

    versions = db.query(mfg.BoM).filter(mfg.BoM.unit_id == unit_id).count()
    bom = mfg.BoM(
        unit_id=unit_id, version=versions + 1,
        status="APPROVED" if approve else "DRAFT",
        created_by=actor.id,
    )
    db.add(bom)
    db.flush()
    for ln in lines:
        db.add(mfg.BoMLine(
            bom_id=bom.id,
            item_code=ln.get("item_code"),
            description=ln.get("description", ""),
            quantity=float(ln.get("quantity", 0)),
            uom=ln.get("uom", ""),
            estimated_unit_price=float(ln.get("estimated_unit_price", 0)),
        ))
    audit.record(db, actor=actor.username, event="bom.created",
                 entity_type="bom", entity_id=bom.id,
                 after={"unit_id": unit_id, "version": bom.version,
                        "status": bom.status, "lines": len(lines)})
    db.commit()
    return bom


def latest_bom(db: Session, unit_id: int, approved_only: bool = False) -> mfg.BoM | None:
    """The costing baseline is the latest APPROVED BoM version. A new draft
    never changes the baseline of an existing order."""
    query = db.query(mfg.BoM).filter(mfg.BoM.unit_id == unit_id)
    if approved_only:
        query = query.filter(mfg.BoM.status == "APPROVED")
    return query.order_by(mfg.BoM.id.desc()).first()


def approve_bom(db: Session, *, actor: User, bom_id: int) -> mfg.BoM:
    if actor.role not in (Role.GM, Role.FINANCE, Role.ADMIN):
        raise PermissionDenied("اعتماد BoM للمدير العام/المالي فقط")
    bom = db.get(mfg.BoM, bom_id)
    if bom is None:
        raise NotFound(f"BoM {bom_id} غير موجود")
    ensure_unit_open(db, bom.unit_id)
    if bom.status != "DRAFT":
        raise DomainError("النسخة معتمدة سابقاً أو ملغاة")
    bom.status = "APPROVED"
    audit.record(db, actor=actor.username, event="bom.approved",
                 entity_type="bom", entity_id=bom.id,
                 after={"version": bom.version, "unit_id": bom.unit_id})
    db.commit()
    return bom


# ---------------------------------------------------------------------------
# Manufacturing orders & work orders
# ---------------------------------------------------------------------------

def create_mo(db: Session, *, actor: User, unit_id: int) -> ManufacturingOrder:
    """Create a MO with chained work orders from the build-method template."""
    if actor.role not in (Role.ENGINEER, Role.ADMIN):
        raise PermissionDenied("إنشاء أوامر التصنيع للمهندسين فقط")
    unit = get_unit_or_404(db, unit_id)
    ensure_unit_open(db, unit_id)
    db.refresh(unit)
    if unit.status in ("DONE", "DELIVERED"):
        raise DomainError("الوحدة منتهية — لا يمكن إنشاء أمر تصنيع جديد")
    # Claim the unit atomically, not just a check-then-insert. Repeated or
    # concurrent creation must not produce a second chain/cost baseline.
    if unit.status != "PLANNED" or db.query(ManufacturingOrder).filter(
        ManufacturingOrder.unit_id == unit_id,
        ManufacturingOrder.status != "CANCELLED",
    ).first():
        raise Conflict("يوجد أمر تصنيع للوحدة؛ لا يمكن تكرار دورة التصنيع")
    claimed = db.execute(
        update(ProductionUnit).where(
            ProductionUnit.id == unit_id, ProductionUnit.status == "PLANNED"
        ).values(status="IN_PRODUCTION")
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise Conflict("بدأ تصنيع الوحدة في جلسة أخرى")

    mo = ManufacturingOrder(unit_id=unit_id, status="CONFIRMED")
    db.add(mo)
    db.flush()

    stages = mfg.STAGE_TEMPLATES[unit.build_method]
    previous_wo: WorkOrder | None = None
    for seq, stage in enumerate(stages, start=1):
        wo = WorkOrder(
            mo_id=mo.id, operation=stage, sequence_no=seq,
            status="PENDING", depends_on_id=previous_wo.id if previous_wo else None,
        )
        db.add(wo)
        db.flush()
        previous_wo = wo

    audit.record(db, actor=actor.username, event="mo.created",
                 entity_type="manufacturing_order", entity_id=mo.id,
                 after={"unit_id": unit_id, "stages": len(stages),
                        "build_method": unit.build_method})
    db.commit()
    return mo


def _wo_or_404(db: Session, wo_id: int) -> WorkOrder:
    wo = db.get(WorkOrder, wo_id)
    if wo is None:
        raise NotFound(f"أمر العمل {wo_id} غير موجود")
    return wo


def start_wo(db: Session, *, actor: User, wo_id: int) -> WorkOrder:
    if actor.role not in (Role.ENGINEER, Role.ADMIN):
        raise PermissionDenied("بدء أوامر العمل للمهندسين فقط")
    wo = _wo_or_404(db, wo_id)
    mo = db.get(ManufacturingOrder, wo.mo_id)
    ensure_unit_open(db, mo.unit_id)
    db.refresh(wo)
    if wo.work_center_id is not None:
        center = db.get(mfg.WorkCenter, wo.work_center_id)
        if center is None or not center.is_active:
            raise DomainError("مركز العمل مفقود أو معطل؛ عدّل خطة المرحلة أولاً")
    if wo.status != "PENDING":
        raise DomainError(f"أمر العمل في حالة {wo.status} — لا يمكن بدءه")
    if wo.depends_on_id is not None:
        dep = _wo_or_404(db, wo.depends_on_id)
        if dep.status != "DONE":
            raise DomainError(
                f"لا يمكن بدء «{wo.operation}» قبل انتهاء «{dep.operation}» "
                "(تبعية مراحل التصنيع)"
            )
    wo.status = "IN_PROGRESS"
    mo = db.get(ManufacturingOrder, wo.mo_id)
    if mo is not None and mo.status == "CONFIRMED":
        mo.status = "IN_PRODUCTION"
    audit.record(db, actor=actor.username, event="wo.started",
                 entity_type="work_order", entity_id=wo.id,
                 after={"operation": wo.operation})
    db.commit()
    return wo


# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------

def report_progress(db: Session, *, actor: User, payload: dict) -> mfg.ProgressEvent:
    """Engineer's daily report. Structured data only — the AI extraction
    port upstream may prefill, never invent. Material shortage spawns a
    linked material request instead of mixing production notes with buying."""
    if actor.role not in (Role.ENGINEER, Role.WAREHOUSE):
        raise PermissionDenied("تقارير التقدم للمهندسين/المستودع فقط")
    from app.modules.manufacturing.reports import event_date
    normalized_date = event_date(payload.get("event_date"))
    stops = []
    for stop in payload.get("stoppages") or []:
        try:
            value = float(stop.get("hours", 0))
        except (ValueError, TypeError) as exc:
            raise DomainError("ساعات التوقف غير صالحة") from exc
        if not math.isfinite(value) or value < 0 or not str(stop.get("reason", "")).strip():
            raise DomainError("التوقف يتطلب ساعات موجبة أو صفر وسبباً موثقاً")
        stops.append({**stop, "hours": value, "reason": str(stop["reason"]).strip()})
    unit = get_unit_or_404(db, payload["unit_id"])
    ensure_unit_open(db, unit.id)
    db.refresh(unit)
    if unit.status in ("DONE", "DELIVERED"):
        raise DomainError("الوحدة مكتملة؛ لا يمكن إضافة تقدم أو عمالة بعد الإقفال")

    wo = None
    if payload.get("work_order_id"):
        wo = _wo_or_404(db, payload["work_order_id"])
        if wo.status not in ("PENDING", "IN_PROGRESS"):
            raise DomainError("المرحلة مكتملة؛ لا يمكن تحميل ساعات إضافية عليها")
        if wo.work_center_id is not None:
            center = db.get(mfg.WorkCenter, wo.work_center_id)
            if center is None or not center.is_active:
                raise DomainError("مركز العمل مفقود أو معطل؛ عدّل خطة المرحلة أولاً")
        if wo.mo_id is None or db.get(ManufacturingOrder, wo.mo_id).unit_id != unit.id:
            raise DomainError("أمر العمل لا ينتمي لوحدة الإنتاج المحددة")
        if wo.status == "PENDING":
            # reporting on a not-yet-started WO implies starting it
            # (dependency guard applies)
            if wo.depends_on_id is not None:
                dep = _wo_or_404(db, wo.depends_on_id)
                if dep.status != "DONE":
                    raise DomainError(
                        f"لا يمكن تسجيل تقدم على «{wo.operation}» قبل انتهاء "
                        f"«{dep.operation}»"
                    )
            wo.status = "IN_PROGRESS"
            mo = db.get(ManufacturingOrder, wo.mo_id)
            if mo is not None and mo.status == "CONFIRMED":
                mo.status = "IN_PRODUCTION"

    percent = float(payload.get("percent_complete", 0) or 0)
    if not math.isfinite(percent) or not (0 <= percent <= 100):
        raise DomainError("نسبة الإنجاز يجب أن تكون بين 0 و 100")
    hours = float(payload.get("hours", 0) or 0)
    if not math.isfinite(hours) or hours < 0:
        raise DomainError("ساعات العمل غير صالحة")

    # labor rate: the WO's work center, else the configured default
    rate = None
    if wo is not None and wo.work_center_id is not None:
        wc = db.get(mfg.WorkCenter, wo.work_center_id)
        if wc is not None:
            rate = wc.hourly_rate
    if rate is None:
        from app.platform.config import get_settings

        rate = get_settings().default_labor_rate or 0.0
    labor_cost = round(hours * rate, 2)

    ev = mfg.ProgressEvent(
        unit_id=unit.id,
        work_order_id=wo.id if wo else None,
        operation=payload.get("operation") or (wo.operation if wo else ""),
        percent_complete=percent,
        workers=int(payload.get("workers", 0) or 0),
        hours=hours,
        stoppages=stops,
        material_shortage=bool(payload.get("material_shortage")),
        shortage_description=payload.get("shortage_description"),
        reported_by=actor.id,
        labor_rate_used=rate,
        labor_cost=labor_cost,
        event_date=normalized_date,
    )

    if wo is not None:
        wo.percent_complete = max(wo.percent_complete, percent)
        wo.actual_hours = round(wo.actual_hours + hours, 2)
        if wo.percent_complete >= 100:
            wo.status = "DONE"
            _propagate_completion(db, wo)

    # material shortage → linked material request (never a purchase by itself)
    if ev.material_shortage:
        mr = mr_service.create_request(
            db,
            requester_id=actor.id,
            payload={
                "production_unit_id": unit.id,
                "item_description": ev.shortage_description or "مادة ناقصة أثناء الإنتاج",
                "quantity": 0,
                "uom": "",
                "need_by_date": "",
                "priority": "STOPPING",
                "purpose": f"نقص مادة أوقف العمل: {ev.operation}",
            },
            raw_message=ev.shortage_description,
            channel="progress",
        )
        ev.linked_material_request_id = mr.id

    db.add(ev)
    db.flush()
    audit.record(db, actor=actor.username, event="progress.reported",
                 entity_type="progress_event", entity_id=ev.id,
                 channel="progress",
                 after={"unit_id": unit.id, "operation": ev.operation,
                        "percent": ev.percent_complete, "hours": ev.hours,
                        "labor_cost": ev.labor_cost,
                        "shortage": ev.material_shortage,
                        "linked_mr": ev.linked_material_request_id})
    db.commit()
    return ev


def _propagate_completion(db: Session, wo: WorkOrder) -> None:
    """All WOs of the MO done → MO DONE → unit DONE."""
    mo = db.get(ManufacturingOrder, wo.mo_id)
    if mo is None or mo.status != "IN_PRODUCTION":
        return
    siblings = db.query(WorkOrder).filter(WorkOrder.mo_id == mo.id).all()
    if all(w.status == "DONE" for w in siblings):
        mo.status = "DONE"
        unit = db.get(ProductionUnit, mo.unit_id)
        if unit is not None:
            unit.status = "DONE"
        audit.record(db, actor="system", event="mo.completed",
                     entity_type="manufacturing_order", entity_id=mo.id,
                     after={"unit_id": mo.unit_id})


# ---------------------------------------------------------------------------
# Material issues (صرف المواد للوحدة)
# ---------------------------------------------------------------------------

def issue_materials(
    db: Session, *, actor: User, unit_id: int, lines: list[dict],
) -> mfg.MaterialIssue:
    if actor.role != Role.WAREHOUSE:
        raise PermissionDenied("صرف المواد لأمين المستودع فقط")
    unit = get_unit_or_404(db, unit_id)
    if not lines:
        raise DomainError("بنود الصرف مطلوبة")

    for ln in lines:
        if not math.isfinite(float(ln.get("quantity", 0))) or ln.get("quantity", 0) <= 0:
            raise DomainError("كمية الصرف يجب أن تكون موجبة")
    # A write lock on the source PO serializes its availability check + issue
    # across units. FOR UPDATE alone is ignored by SQLite; the no-op UPDATE
    # locks on both supported backends and is held until commit/rollback.
    for po_id in sorted({ln.get("purchase_order_id") for ln in lines}):
        db.execute(update(PurchaseOrder).where(PurchaseOrder.id == po_id)
                   .values(status=PurchaseOrder.status))
    ensure_unit_open(db, unit_id)
    db.refresh(unit)
    if unit.status in ("DONE", "DELIVERED"):
        raise DomainError("الوحدة مكتملة؛ لا يمكن صرف مواد إضافية بعد الإقفال")
    pending: dict[tuple[int, str], float] = {}
    for ln in lines:
        po = db.get(PurchaseOrder, ln.get("purchase_order_id"))
        if po is None:
            raise DomainError(f"بند صرف بلا أمر شراء صالح: {ln.get('description', '')}")
        key = ln.get("item_code") or ln.get("description", "")
        po_line = next(
            (p for p in po.lines
             if (p.get("item_code") or p.get("description", "")) == key), None
        )
        if po_line is None:
            raise DomainError(f"البند '{key}' غير موجود في أمر الشراء {po.po_no}")
        if po_line.get("uom") and ln.get("uom", "") != po_line["uom"]:
            raise DomainError(
                f"وحدة قياس الصرف '{ln['uom']}' لا تطابق أمر الشراء "
                f"'{po_line['uom']}' للبند '{key}' — يلزم معامل تحويل معتمد"
            )
        # availability: accepted qty − already issued on this PO
        accepted = receiving.accepted_qty_by_item(po.id, db).get(key, 0.0)
        already = _issued_qty_by_item(db, po.id).get(key, 0.0)
        reserved = pending.get((po.id, key), 0)
        if ln["quantity"] > accepted - already - reserved + 1e-9:
            raise DomainError(
                f"كمية الصرف تتجاوز المتاح: البند '{key}' المقبول {accepted} "
                f"والمصروف سابقاً {already}"
            )
        pending[(po.id, key)] = reserved + ln["quantity"]
        # cost at the PO price actually agreed — never invented
        ln["unit_price"] = po_line["unit_price"]

    issue = mfg.MaterialIssue(
        issue_no=next_number(db, "MI"),
        unit_id=unit_id,
        issued_by=actor.id,
        lines=lines,
    )
    db.add(issue)
    db.flush()
    audit.record(db, actor=actor.username, event="materials.issued",
                 entity_type="material_issue", entity_id=issue.id,
                 after={"issue_no": issue.issue_no, "unit_id": unit_id,
                        "lines": len(lines),
                        "materials_cost": issue.materials_cost()})
    db.commit()
    return issue


def _issued_qty_by_item(db: Session, po_id: int) -> dict[str, float]:
    totals: dict[str, float] = {}
    issues = db.query(mfg.MaterialIssue).all()
    for issue in issues:
        for ln in issue.lines:
            if ln.get("purchase_order_id") == po_id:
                key = ln.get("item_code") or ln.get("description", "")
                totals[key] = totals.get(key, 0) + ln.get("quantity", 0)
    return totals


# ---------------------------------------------------------------------------
# Unit progress view
# ---------------------------------------------------------------------------

def unit_progress(db: Session, unit_id: int) -> dict:
    unit = get_unit_or_404(db, unit_id)
    mos = (
        db.query(ManufacturingOrder)
        .filter(ManufacturingOrder.unit_id == unit_id)
        .order_by(ManufacturingOrder.id)
        .all()
    )
    wo_rows = []
    for mo in mos:
        wos = (
            db.query(WorkOrder)
            .filter(WorkOrder.mo_id == mo.id)
            .order_by(WorkOrder.sequence_no)
            .all()
        )
        for wo in wos:
            blocked = (
                wo.depends_on_id is not None
                and _wo_or_404(db, wo.depends_on_id).status != "DONE"
            )
            wo_rows.append({
                "id": wo.id, "operation": wo.operation,
                "sequence": wo.sequence_no, "status": wo.status,
                "percent": wo.percent_complete,
                "estimated_hours": wo.estimated_hours,
                "actual_hours": wo.actual_hours,
                "blocked_by_dependency": wo.status == "PENDING" and blocked,
            })
    overall = (
        round(sum(r["percent"] for r in wo_rows) / len(wo_rows), 1)
        if wo_rows else 0.0
    )
    shortages = (
        db.query(mfg.ProgressEvent)
        .filter(
            mfg.ProgressEvent.unit_id == unit_id,
            mfg.ProgressEvent.material_shortage,
        )
        .all()
    )
    return {
        "unit_id": unit_id,
        "house_no": unit.house_no,
        "build_method": unit.build_method,
        "overall_percent": overall,
        "work_orders": wo_rows,
        "open_material_shortages": [
            {"event_id": s.id, "description": s.shortage_description,
             "linked_mr": s.linked_material_request_id}
            for s in shortages
        ],
    }
