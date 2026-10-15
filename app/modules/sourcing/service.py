"""Sourcing application service: cases, quotation versions, comparison."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.material_requests import service as mr_service
from app.modules.sourcing.models import Quotation, SourcingCase
from app.platform.config import get_settings
from app.shared.errors import DomainError, NotFound, PermissionDenied
from app.workflow import states


def create_case(db: Session, *, owner_id: int, mr_ids: list[int]) -> SourcingCase:
    for mid in mr_ids:
        mr = mr_service.get_or_404(db, mid)
        if mr.status not in (states.SOURCING, states.QUOTATIONS_RECEIVED):
            raise DomainError(
                f"الطلب {mr.request_no} في حالة {mr.status} — لا يمكن فتح Sourcing Case"
            )
    case = SourcingCase(material_request_ids=mr_ids, owner_id=owner_id)
    db.add(case)
    db.flush()
    audit.record(
        db, actor=str(owner_id), event="sourcing.case.opened",
        entity_type="sourcing_case", entity_id=case.id,
        after={"material_request_ids": mr_ids},
    )
    return case


def add_quotation(db: Session, *, case_id: int, payload: dict) -> Quotation:
    case = db.get(SourcingCase, case_id)
    if case is None:
        raise NotFound(f"Sourcing Case {case_id} غير موجود")

    supplier = payload.get("supplier") or ""
    if not supplier:
        raise DomainError("اسم المورد إلزامي")
    lines = payload.get("lines") or []
    if not lines:
        raise DomainError("عرض السعر يجب أن يحتوي بنوداً")
    for line in lines:
        if line.get("quantity", 0) <= 0 or line.get("unit_price", 0) < 0:
            raise DomainError("كمية أو سعر غير صالح في البنود")

    versions = (
        db.query(Quotation)
        .filter(Quotation.sourcing_case_id == case_id, Quotation.supplier == supplier)
        .count()
    )
    q = Quotation(
        sourcing_case_id=case_id,
        supplier=supplier,
        supplier_vat=payload.get("supplier_vat", ""),
        version=versions + 1,
        currency=payload.get("currency", "SAR"),
        lines=lines,
        shipping_cost=float(payload.get("shipping_cost", 0)),
        extra_costs=float(payload.get("extra_costs", 0)),
        non_refundable_tax=float(payload.get("non_refundable_tax", 0)),
        discount=float(payload.get("discount", 0)),
        delivery_days=int(payload.get("delivery_days", 0)),
        payment_terms=payload.get("payment_terms", ""),
        valid_until=payload.get("valid_until", ""),
    )
    db.add(q)
    db.flush()

    # any quotation received moves linked requests to QUOTATIONS_RECEIVED
    for mid in case.material_request_ids:
        mr = mr_service.get_or_404(db, mid)
        if mr.status == states.SOURCING:
            mr.status = states.QUOTATIONS_RECEIVED
            audit.record(
                db, actor=str(case.owner_id), event="status.changed",
                entity_type="material_request", entity_id=mr.id,
                before={"status": states.SOURCING},
                after={"status": states.QUOTATIONS_RECEIVED},
            )
    audit.record(
        db, actor=str(case.owner_id), event="quotation.received",
        entity_type="quotation", entity_id=q.id,
        after={"supplier": q.supplier, "version": q.version,
               "comparative_cost": q.comparative_cost()},
    )
    return q


def quotation_snapshot_hash(q: Quotation) -> str:
    """Hash of the exact quotation content — approvals bind to this."""
    return hashlib.sha256(
        json.dumps(q.snapshot_dict(), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def compare(db: Session, case_id: int) -> dict:
    """Side-by-side comparison; never auto-selects the cheapest."""
    case = db.get(SourcingCase, case_id)
    if case is None:
        raise NotFound(f"Sourcing Case {case_id} غير موجود")
    quotes = sorted(
        db.query(Quotation).filter(Quotation.sourcing_case_id == case_id).all(),
        key=lambda q: q.comparative_cost(),
    )
    rows = [
        {
            "quotation_id": q.id,
            "supplier": q.supplier,
            "version": q.version,
            "currency": q.currency,
            "materials_total": q.materials_total(),
            "shipping": q.shipping_cost,
            "non_refundable_tax": q.non_refundable_tax,
            "extra_costs": q.extra_costs,
            "discount": q.discount,
            "comparative_cost": q.comparative_cost(),
            "delivery_days": q.delivery_days,
            "payment_terms": q.payment_terms,
            "lines": q.lines,
            "snapshot_hash": quotation_snapshot_hash(q),
        }
        for q in quotes
    ]
    return {
        "case_id": case_id,
        "min_quotations_required": get_settings().min_quotations,
        "quotations": rows,
    }


def mark_pending_gm(db: Session, case_id: int, actor: str) -> dict:
    """Move linked requests to PENDING_GM_APPROVAL after comparison review."""
    case = db.get(SourcingCase, case_id)
    if case is None:
        raise NotFound(f"Sourcing Case {case_id} غير موجود")
    settings = get_settings()
    quotes = db.query(Quotation).filter(Quotation.sourcing_case_id == case_id).all()
    single_supplier = len({q.supplier for q in quotes}) < settings.min_quotations
    for mid in case.material_request_ids:
        mr = mr_service.get_or_404(db, mid)
        states.assert_transition(mr.status, states.PENDING_GM_APPROVAL)
        if single_supplier:
            audit.record(
                db, actor=actor, event="sourcing.single_supplier_flag",
                entity_type="material_request", entity_id=mr.id,
                result="rejected",
                after={"note": "مورد واحد دون مقارنة — يتطلب موافقة استثناء موثقة"},
            )
            raise DomainError(
                "عدد العروض أقل من الحد المطلوب — لا يمكن العرض على المدير دون موافقة استثناء"
            )
        mr.status = states.PENDING_GM_APPROVAL
        audit.record(
            db, actor=actor, event="status.changed",
            entity_type="material_request", entity_id=mr.id,
            before={"status": states.QUOTATIONS_RECEIVED},
            after={"status": states.PENDING_GM_APPROVAL},
        )
    db.commit()
    return {"case_id": case_id, "status": "PENDING_GM_APPROVAL"}
