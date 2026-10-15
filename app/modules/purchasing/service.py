"""Purchasing service: PO issuance (approval-bound) and payment requests."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.approvals import service as approvals
from app.modules.audit import service as audit
from app.modules.material_requests import service as mr_service
from app.modules.purchasing.models import PaymentRequest, PurchaseOrder
from app.platform.security import Role, User
from app.shared.errors import DomainError, InvalidTransition, NotFound, PermissionDenied
from app.shared.ids import next_number
from app.workflow import states


def issue_po(db: Session, *, mr_id: int, actor: User) -> PurchaseOrder:
    """Issue a PO only from the approved snapshot. No approval → no PO."""
    mr = mr_service.get_or_404(db, mr_id)
    if mr.status != states.APPROVED:
        raise InvalidTransition(
            f"الطلب في حالة {mr.status} — يجب أن يكون APPROVED قبل إصدار أمر الشراء"
        )
    approval = approvals.validate_approval_for_po(db, mr_id)

    from app.modules.sourcing.models import Quotation
    q = db.get(Quotation, approval.quotation_id)
    if q is None:
        raise NotFound("العرض المعتمد غير موجود")

    po = PurchaseOrder(
        po_no=next_number(db, "PO"),
        material_request_id=mr_id,
        approval_id=approval.id,
        supplier=q.supplier,
        currency=q.currency,
        lines=q.lines,
        shipping_cost=q.shipping_cost,
        extra_costs=q.extra_costs,
        non_refundable_tax=q.non_refundable_tax,
        discount=q.discount,
        delivery_days=q.delivery_days,
        payment_terms=q.payment_terms,
        status="ISSUED",
    )
    db.add(po)
    db.flush()

    states.assert_transition(mr.status, states.PO_ISSUED)
    mr.status = states.PO_ISSUED
    audit.record(
        db, actor=actor.username, event="po.issued",
        entity_type="purchase_order", entity_id=po.id,
        after={"po_no": po.po_no, "supplier": po.supplier,
               "total": po.total(), "approval_id": approval.id,
               "material_request_id": mr_id},
    )
    audit.record(
        db, actor=actor.username, event="status.changed",
        entity_type="material_request", entity_id=mr_id,
        before={"status": states.APPROVED}, after={"status": states.PO_ISSUED},
    )
    db.commit()
    return po


# ---- Payment requests -----------------------------------------------------

def create_payment_request(
    db: Session, *, po_id: int, actor: User, amount: float, purpose: str,
    method: str = "bank", beneficiary: str = "",
) -> PaymentRequest:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound(f"أمر الشراء {po_id} غير موجود")
    if actor.role not in (Role.PROCUREMENT, Role.ACCOUNTANT, Role.FINANCE):
        raise PermissionDenied("المشتريات/المحاسبة/المالية فقط تطلب الدفع")
    if amount <= 0 or amount > po.total():
        raise DomainError("مبلغ الدفع غير صالح (صفر أو يتجاوز قيمة أمر الشراء)")

    pr = PaymentRequest(
        pay_no=next_number(db, "PAY"),
        purchase_order_id=po_id,
        requested_by=actor.id,
        amount=amount,
        currency=po.currency,
        beneficiary=beneficiary or po.supplier,
        method=method,
        purpose=purpose,
        status="PENDING_APPROVAL",
    )
    db.add(pr)
    db.flush()

    mr = mr_service.get_or_404(db, po.material_request_id)
    states.assert_transition(mr.status, states.PAYMENT_PENDING)
    mr.status = states.PAYMENT_PENDING
    audit.record(
        db, actor=actor.username, event="payment.requested",
        entity_type="payment_request", entity_id=pr.id,
        after={"pay_no": pr.pay_no, "amount": amount, "purpose": purpose},
    )
    audit.record(
        db, actor=actor.username, event="status.changed",
        entity_type="material_request", entity_id=po.material_request_id,
        before={"status": states.PO_ISSUED}, after={"status": states.PAYMENT_PENDING},
    )
    db.commit()
    return pr


def approve_payment(db: Session, *, pay_id: int, actor: User) -> PaymentRequest:
    pr = db.get(PaymentRequest, pay_id)
    if pr is None:
        raise NotFound(f"طلب الدفع {pay_id} غير موجود")
    if actor.role not in (Role.GM, Role.FINANCE):
        raise PermissionDenied("الموافقة على الدفع للمدير العام/المالي فقط")
    if actor.id == pr.requested_by:
        raise PermissionDenied("فصل صلاحيات: طالب الدفع لا يعتمده")
    _payment_transition(pr, "APPROVED")
    pr.approved_by = actor.id
    audit.record(db, actor=actor.username, event="payment.approved",
                 entity_type="payment_request", entity_id=pr.id,
                 after={"amount": pr.amount, "status": pr.status})
    db.commit()
    return pr


def record_bank_execution(
    db: Session, *, pay_id: int, actor: User, bank_ref: str, outcome: str = "PAID",
) -> PaymentRequest:
    """A bank receipt image is NOT a payment approval. Execution happens only
    after approval, and reconciliation requires a statement/bank confirmation."""
    pr = db.get(PaymentRequest, pay_id)
    if pr is None:
        raise NotFound(f"طلب الدفع {pay_id} غير موجود")
    if actor.role != Role.ACCOUNTANT:
        raise PermissionDenied("تسجيل التنفيذ البنكي للمحاسب فقط")
    if pr.status != "APPROVED":
        raise DomainError("لا يُسجَّل تنفيذ قبل الموافقة — إيصال البنك ليس موافقة")
    if outcome == "PAID" and pr.status == "APPROVED":
        # execution passes through PROCESSING before PAID
        _payment_transition(pr, "PROCESSING")
    _payment_transition(pr, outcome)
    pr.bank_ref = bank_ref
    audit.record(db, actor=actor.username, event="payment.executed",
                 entity_type="payment_request", entity_id=pr.id,
                 after={"bank_ref": bank_ref, "outcome": outcome})
    db.commit()
    return pr


def reconcile_payment(db: Session, *, pay_id: int, actor: User) -> PaymentRequest:
    pr = db.get(PaymentRequest, pay_id)
    if pr is None:
        raise NotFound(f"طلب الدفع {pay_id} غير موجود")
    if actor.role != Role.ACCOUNTANT:
        raise PermissionDenied("المطابقة البنكية للمحاسب فقط")
    _payment_transition(pr, "RECONCILED")
    audit.record(db, actor=actor.username, event="payment.reconciled",
                 entity_type="payment_request", entity_id=pr.id)
    db.commit()
    return pr


def _payment_transition(pr: PaymentRequest, target: str) -> None:
    allowed = PaymentRequest.PAYMENT_TRANSITIONS.get(pr.status, set())
    if target not in allowed:
        raise DomainError(f"انتقال دفع غير مسموح: {pr.status} → {target}")
    pr.status = target
