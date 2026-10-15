"""Receiving service: full / partial / over / damaged / rejected receipts.

Rules:
- receipt lines record required vs supplied vs accepted vs rejected.
- any rejected/damaged quantity that is not fully offset puts the request
  on QC_HOLD; nothing is billable unless it was ACCEPTED here.
- the requester (engineer) may register a preliminary receipt, but only
  warehouse/QC confirm the accepted quantities.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.material_requests import service as mr_service
from app.modules.purchasing.models import PurchaseOrder
from app.modules.receiving.models import GoodsReceipt
from app.platform.security import Role, User
from app.shared.errors import DomainError, NotFound, PermissionDenied
from app.shared.ids import next_number
from app.workflow import states


def create_grn(
    db: Session, *, po_id: int, actor: User, lines: list[dict],
    site: str = "", photos: list[str] | None = None,
) -> GoodsReceipt:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound(f"أمر الشراء {po_id} غير موجود")
    if actor.role not in (Role.WAREHOUSE, Role.ENGINEER):
        raise PermissionDenied("الاستلام لأمين المستودع أو المهندس فقط")
    from app.modules.manufacturing.financial_guards import ensure_po_open
    ensure_po_open(db, po_id)

    mr = mr_service.get_or_404(db, po.material_request_id)
    if mr.status == states.PO_ISSUED:
        # receiving implies delivery started
        states.assert_transition(mr.status, states.DELIVERY_PENDING)
        mr.status = states.DELIVERY_PENDING
    if mr.status not in (states.DELIVERY_PENDING, states.PAYMENT_PENDING,
                         states.PARTIALLY_RECEIVED):
        raise DomainError(
            f"الطلب في حالة {mr.status} — الاستلام يتطلب DELIVERY/PARTIALLY_RECEIVED"
        )

    if not lines:
        raise DomainError("بنود الاستلام مطلوبة")
    for ln in lines:
        for k in ("qty_required", "qty_supplied", "qty_accepted", "qty_rejected"):
            if ln.get(k) is None:
                raise DomainError(f"الحقل {k} مطلوب في كل بند استلام")
        if ln["qty_accepted"] + ln["qty_rejected"] > ln["qty_supplied"]:
            raise DomainError("المقبول + المرفوض لا يتجاوز المورد فعلياً")

    total_required = sum(ln["qty_required"] for ln in lines)
    total_accepted = sum(ln["qty_accepted"] for ln in lines)
    total_rejected = sum(ln["qty_rejected"] for ln in lines)

    if total_rejected > 0:
        inspection = "QC_HOLD"
    elif total_accepted < total_required:
        inspection = "PARTIAL"
    else:
        inspection = "ACCEPTED"

    grn = GoodsReceipt(
        grn_no=next_number(db, "GRN"),
        purchase_order_id=po_id,
        supplier=po.supplier,
        site=site,
        received_by=actor.id,
        lines=lines,
        photos=photos or [],
        inspection_result=inspection,
    )
    db.add(grn)
    db.flush()

    if inspection == "QC_HOLD":
        target = states.QC_HOLD
    elif total_accepted < total_required:
        target = states.PARTIALLY_RECEIVED
    else:
        target = states.RECEIVED
    states.assert_transition(mr.status, target)
    mr.status = target

    audit.record(
        db, actor=actor.username, event="grn.created",
        entity_type="goods_receipt", entity_id=grn.id,
        after={"grn_no": grn.grn_no, "inspection": inspection,
               "accepted": total_accepted, "rejected": total_rejected,
               "required": total_required},
    )
    audit.record(
        db, actor=actor.username, event="status.changed",
        entity_type="material_request", entity_id=mr.id,
        after={"status": target, "grn_no": grn.grn_no},
    )
    db.commit()
    return grn


def accepted_qty_by_item(po_id: int, db: Session) -> dict[str, float]:
    """Sum of accepted quantities per item across all GRNs of a PO.
    Quantities under quality hold (QC_HOLD) are excluded — quarantined
    stock is not available for issue or billing."""
    grns = db.query(GoodsReceipt).filter(GoodsReceipt.purchase_order_id == po_id).all()
    totals: dict[str, float] = {}
    for grn in grns:
        if grn.inspection_result == "QC_HOLD":
            continue
        for ln in grn.lines:
            key = ln.get("item_code") or ln.get("description", "")
            totals[key] = totals.get(key, 0) + (ln.get("qty_accepted") or 0)
    return totals
