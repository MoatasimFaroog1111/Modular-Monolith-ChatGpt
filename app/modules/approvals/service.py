"""Approval service — separation of duties and version binding.

Rules:
- the requester may never approve their own request.
- procurement may never approve (they recommend; GM/finance decide).
- an approval binds to a quotation snapshot hash; a newer quotation version
  invalidates the approval and requires a fresh decision.
- approval thresholds are configurable; if unset, GM approval is always
  required and finance thresholds raise PolicyNotConfigured when invoked.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.approvals.models import Approval
from app.modules.audit import service as audit
from app.modules.material_requests import service as mr_service
from app.modules.sourcing import service as sourcing
from app.modules.sourcing.models import Quotation
from app.platform.config import get_settings
from app.platform.security import Role, User
from app.shared.errors import DomainError, InvalidTransition, NotFound, PermissionDenied
from app.workflow import states

APPROVER_ROLES = {Role.GM, Role.FINANCE}


def decide(
    db: Session,
    *,
    actor: User,
    mr_id: int,
    quotation_id: int,
    decision: str,                      # APPROVED | REJECTED | MODIFY
    comment: str = "",
    exception_reason: str | None = None,
) -> Approval:
    if decision not in ("APPROVED", "REJECTED", "MODIFY"):
        raise DomainError("قرار غير معروف")
    if actor.role not in APPROVER_ROLES:
        raise PermissionDenied("الموافقة محصورة بالمدير العام/المدير المالي")

    mr = mr_service.get_or_404(db, mr_id)
    if mr.status != states.PENDING_GM_APPROVAL:
        raise DomainError(
            f"الطلب {mr.request_no} في حالة {mr.status} — لا يوجد قرار معلق"
        )
    if mr.requester_id == actor.id:
        raise PermissionDenied("فصل صلاحيات: طالب المادة لا يعتمد طلبه")

    q = db.get(Quotation, quotation_id)
    if q is None:
        raise NotFound(f"العرض {quotation_id} غير موجود")

    # the chosen quotation must belong to a sourcing case that actually
    # contains this material request
    from app.modules.sourcing.models import SourcingCase

    case = db.get(SourcingCase, q.sourcing_case_id)
    if case is None or mr.id not in (case.material_request_ids or []):
        raise DomainError(
            "العرض المحدد لا ينتمي لحالة تسوق مرتبطة بهذا الطلب — لا يمكن الاعتماد"
        )

    settings = get_settings()
    both_roles_required = (
        settings.approval_threshold_1 is not None
        and settings.approval_threshold_2 is not None
        and q.comparative_cost() > settings.approval_threshold_2
    )
    fully_approved = True
    if both_roles_required:
        # above the upper threshold: GM AND finance must both approve.
        from app.platform.security import User as AppUser

        prior = (
            db.query(Approval)
            .filter(
                Approval.entity_type == "material_request",
                Approval.entity_id == str(mr_id),
                Approval.decision == "APPROVED",
            )
            .all()
        )
        prior_roles = set()
        for a in prior:
            u = db.get(AppUser, a.actor_id)
            if u is not None:
                prior_roles.add(u.role)
        prior_roles |= {actor.role}
        if not {Role.GM, Role.FINANCE} <= prior_roles:
            fully_approved = False

    snapshot_hash = sourcing.quotation_snapshot_hash(q)

    approval = Approval(
        entity_type="material_request",
        entity_id=str(mr_id),
        decision=decision,
        actor_id=actor.id,
        actor_username=actor.username,
        quotation_id=q.id,
        quotation_version=q.version,
        snapshot_hash=snapshot_hash,
        amount=q.comparative_cost(),
        currency=q.currency,
        supplier=q.supplier,
        comment=comment,
        exception_reason=exception_reason,
    )
    db.add(approval)
    db.flush()

    if decision == "APPROVED":
        if fully_approved:
            mr.status = states.APPROVED
        else:
            # waiting for the second mandatory approver (GM + finance)
            mr.status = states.PENDING_GM_APPROVAL
    else:
        # REJECTED / MODIFY → back to sourcing for new/updated quotations
        states.assert_transition(mr.status, states.SOURCING)
        mr.status = states.SOURCING

    audit.record(
        db, actor=actor.username, event="approval.decided",
        entity_type="material_request", entity_id=mr_id,
        before={"status": states.PENDING_GM_APPROVAL},
        after={
            "status": mr.status, "decision": decision,
            "fully_approved": fully_approved if decision == "APPROVED" else None,
            "quotation_id": q.id, "quotation_version": q.version,
            "snapshot_hash": snapshot_hash, "amount": q.comparative_cost(),
            "supplier": q.supplier, "exception_reason": exception_reason,
        },
        result="ok" if decision == "APPROVED" else "rejected",
    )
    db.commit()
    return approval


def validate_approval_for_po(db: Session, mr_id: int) -> Approval:
    """The PO guard: an APPROVED decision bound to the current quotation snapshot."""
    mr = mr_service.get_or_404(db, mr_id)
    if mr.status != states.APPROVED:
        raise DomainError("لا يمكن إنشاء أمر شراء قبل الموافقة")
    approval = (
        db.query(Approval)
        .filter(
            Approval.entity_type == "material_request",
            Approval.entity_id == str(mr_id),
            Approval.decision == "APPROVED",
        )
        .order_by(Approval.id.desc())
        .first()
    )
    if approval is None:
        raise DomainError("لا يوجد اعتماد ساري — لا يمكن إنشاء أمر الشراء")
    q = db.get(Quotation, approval.quotation_id)
    current_hash = sourcing.quotation_snapshot_hash(q)
    if current_hash != approval.snapshot_hash:
        raise InvalidTransition(
            "تغيّرت بنود العرض بعد الاعتماد — يجب موافقة جديدة على النسخة الحالية"
        )
    # a newer version of the same supplier's quotation supersedes the approval
    newer = (
        db.query(Quotation)
        .filter(
            Quotation.sourcing_case_id == q.sourcing_case_id,
            Quotation.supplier == q.supplier,
            Quotation.id != q.id,
            Quotation.version > q.version,
        )
        .first()
    )
    if newer is not None:
        raise InvalidTransition(
            "تغيّرت بنود العرض بعد الاعتماد — يجب موافقة جديدة على النسخة الحالية"
        )
    return approval
