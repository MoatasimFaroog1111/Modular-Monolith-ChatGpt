"""Actual costing: what each production unit really cost.

تكلفة البيت = مواد فعلية (من صرف المواد بسعر أمر الشراء الفعلي)
+ عمالة فعلية (من تقارير Progress × سعر مركز العمل)
+ تكاليف غير مباشرة (نسبة قابلة للإعداد، موثقة ولا تُخترع).

Deviation is always measured against the approved BoM + estimated hours.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.manufacturing import models as mfg
from app.modules.manufacturing import service as mfg_service
from app.modules.projects.models import ManufacturingOrder, WorkOrder
from app.modules.purchasing.models import PurchaseOrder
from app.modules.manufacturing.landed_costs import unit_landed_cost
from app.modules.manufacturing.settlements import material_adjustments
from app.modules.manufacturing.expenses import unit_expenses
from app.platform.config import get_settings
from app.shared.errors import NotFound


def unit_cost_summary(db: Session, unit_id: int) -> dict:
    from app.modules.manufacturing.financial_close import frozen_summary
    frozen = frozen_summary(db, unit_id)
    if frozen is not None:
        return frozen
    unit = mfg_service.get_unit_or_404(db, unit_id)

    # ---- estimated side: latest APPROVED BoM + estimated WO hours -------
    bom = mfg_service.latest_bom(db, unit_id, approved_only=True)
    bom_lines = []
    est_materials = 0.0
    if bom is not None:
        lines = db.query(mfg.BoMLine).filter(mfg.BoMLine.bom_id == bom.id).all()
        for ln in lines:
            est_materials += ln.quantity * ln.estimated_unit_price
            bom_lines.append({
                "item_code": ln.item_code, "description": ln.description,
                "quantity": ln.quantity, "uom": ln.uom,
                "estimated_unit_price": ln.estimated_unit_price,
            })
    est_materials = round(est_materials, 2)

    # estimated labor: WO estimated hours × work-center rate (or default)
    wos = _unit_work_orders(db, unit_id)
    default_rate = get_settings().default_labor_rate or 0.0
    est_labor = 0.0
    for wo in wos:
        rate = _wo_rate(db, wo)
        if rate is None:
            rate = default_rate
        est_labor += wo.estimated_hours * rate
    est_labor = round(est_labor, 2)

    # ---- actual side ----------------------------------------------------
    # NOTE: material cost is PROVISIONAL, valued at PO prices until the
    # supplier invoice settles (invoice matching is the AP module's job).
    issues = db.query(mfg.MaterialIssue).filter(mfg.MaterialIssue.unit_id == unit_id).all()
    currencies = set()
    for i in issues:
        for ln in i.lines:
            po = db.get(PurchaseOrder, ln.get("purchase_order_id"))
            if po is not None:
                currencies.add(po.currency)
    provisional_materials = round(sum(i.materials_cost() for i in issues), 2)
    settlement = (material_adjustments(db, unit_id) if all(c == "SAR" for c in currencies)
                  else {"adjustment_cents": 0, "fully_settled": False, "sources": []})
    act_materials = round(provisional_materials + settlement["adjustment_cents"] / 100, 2)
    events = db.query(mfg.ProgressEvent).filter(mfg.ProgressEvent.unit_id == unit_id).all()
    act_labor = round(sum(e.labor_cost for e in events), 2)
    act_hours = round(sum(e.hours for e in events), 2)
    labor_rate_missing = any(
        e.labor_cost == 0 and e.hours > 0 for e in events
    )
    landed = unit_landed_cost(db, unit_id)
    expenses = unit_expenses(db, unit_id)
    # Labor, budgets and landed charges are SAR. Never add foreign PO values.
    unsupported_currency = any(c != "SAR" for c in currencies)

    overhead = 0.0
    overhead_pct = get_settings().overhead_pct
    if overhead_pct is not None:
        overhead = round((act_materials + act_labor) * overhead_pct / 100.0, 2)

    est_total = round(est_materials + est_labor, 2)
    act_total = (None if unsupported_currency else
                 round(act_materials + act_labor + overhead + landed["total"] + expenses["total"], 2))

    return {
        "unit_id": unit_id,
        "house_no": unit.house_no,
        "build_method": unit.build_method,
        "bom_version": bom.version if bom else None,
        "estimated": {
            "materials": est_materials,
            "labor": est_labor,
            "total": est_total,
        },
        "actual": {
            "materials": None if unsupported_currency else act_materials,
            "labor": act_labor,
            "overhead": None if unsupported_currency else overhead,
            "overhead_pct": overhead_pct,
            "freight": landed["freight"],
            "customs": landed["customs"],
            "landed_cost": landed["total"],
            "direct_expenses": expenses["DIRECT"],
            "equipment": expenses["EQUIPMENT"],
            "total": act_total,
            "labor_hours": act_hours,
        },
        "deviation": {
            "materials": None if unsupported_currency else round(act_materials - est_materials, 2),
            "labor": round(act_labor - est_labor, 2),
            "total": round(act_total - est_total, 2) if act_total is not None else None,
        },
        "currency": "SAR",
        "landed_cost_sources": landed["sources"],
        "expense_sources": expenses["sources"],
        "material_settlement_sources": settlement["sources"],
        "material_variance": settlement["adjustment_cents"] / 100,
        "provisional_materials": provisional_materials,
        "valuation": ("settled_invoice_price" if issues and settlement["fully_settled"]
                      else "provisional_po_price"),
        "currencies": sorted(currencies),
        "flags": {
            "labor_rate_missing": labor_rate_missing,
            "no_approved_bom": bom is None,
            "mixed_currencies": len(currencies) > 1,
            "currency_conversion_required": unsupported_currency,
            "materials_unsettled": bool(issues) and not settlement["fully_settled"],
        },
    }


def cost_deviation_dashboard(db: Session) -> dict:
    """GM view: cost deviation of every unit against its BoM."""
    from app.modules.projects.models import ProductionUnit

    units = db.query(ProductionUnit).order_by(ProductionUnit.id).all()
    rows = []
    for u in units:
        summary = unit_cost_summary(db, u.id)
        rows.append({
            "unit_id": u.id, "house_no": u.house_no,
            "build_method": u.build_method,
            "overall_percent": mfg_service.unit_progress(db, u.id)["overall_percent"],
            "estimated_total": summary["estimated"]["total"],
            "actual_total": summary["actual"]["total"],
            "deviation_total": summary["deviation"]["total"],
            "deviation_pct": round(
                summary["deviation"]["total"] / summary["estimated"]["total"] * 100, 1
            ) if summary["estimated"]["total"] and summary["deviation"]["total"] is not None else None,
            "flags": summary["flags"],
        })
    return {"units": rows}


def _unit_work_orders(db: Session, unit_id: int) -> list[WorkOrder]:
    mos = (
        db.query(ManufacturingOrder)
        .filter(ManufacturingOrder.unit_id == unit_id)
        .all()
    )
    out: list[WorkOrder] = []
    for mo in mos:
        out.extend(
            db.query(WorkOrder).filter(WorkOrder.mo_id == mo.id).all()
        )
    return out


def _wo_rate(db: Session, wo: WorkOrder) -> float | None:
    if wo.work_center_id is None:
        return None
    wc = db.get(mfg.WorkCenter, wo.work_center_id)
    return wc.hourly_rate if wc else None


def factory_dashboard(db: Session) -> dict:
    """لوحة المصنع: تقدم الوحدات، المراحل المتوقفة، المواد الناقصة، ساعات العمل."""
    from app.modules.projects.models import ProductionUnit

    units = db.query(ProductionUnit).order_by(ProductionUnit.id).all()
    rows = []
    for u in units:
        progress = mfg_service.unit_progress(db, u.id)
        wos = _unit_work_orders(db, u.id)
        est_hours = round(sum(w.estimated_hours for w in wos), 2)
        act_hours = round(sum(w.actual_hours for w in wos), 2)
        events = (
            db.query(mfg.ProgressEvent)
            .filter(mfg.ProgressEvent.unit_id == u.id)
            .all()
        )
        stoppage_hours = round(
            sum(
                float(s.get("hours", 0))
                for e in events for s in (e.stoppages or [])
            ), 2
        )
        rows.append({
            "unit_id": u.id, "house_no": u.house_no,
            "build_method": u.build_method,
            "overall_percent": progress["overall_percent"],
            "stages": progress["work_orders"],
            "open_material_shortages": progress["open_material_shortages"],
            "estimated_hours": est_hours,
            "actual_hours": act_hours,
            "stoppage_hours": stoppage_hours,
        })
    return {"units": rows}
