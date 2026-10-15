"""Material-request application service: create → validate → source.

Business guards (per blueprint):
- SoD: requester cannot approve their own request (enforced in approvals).
- duplicate requests within a short window are flagged for review.
- the flow may never skip states (e.g. QUOTATIONS_RECEIVED → PAYMENT_PENDING).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.material_requests.models import MaterialRequest
from app.platform.config import get_settings
from app.shared.errors import DomainError, NotFound, PermissionDenied
from app.shared.ids import next_number
from app.workflow import states


def _change_status(db: Session, mr: MaterialRequest, target: str, actor: str) -> None:
    states.assert_transition(mr.status, target)
    before = mr.status
    mr.status = target
    audit.record(
        db,
        actor=actor,
        event="status.changed",
        entity_type="material_request",
        entity_id=mr.id,
        before={"status": before},
        after={"status": target},
    )


def create_request(
    db: Session,
    *,
    requester_id: int,
    payload: dict,
    raw_message: str | None = None,
    channel: str = "api",
) -> MaterialRequest:
    """Create a DRAFT request. Drafts never carry commercial decisions."""
    mr = MaterialRequest(
        request_no=next_number(db, "MR"),
        requester_id=requester_id,
        project_id=payload.get("project_id"),
        production_unit_id=payload.get("production_unit_id"),
        manufacturing_order_id=payload.get("manufacturing_order_id"),
        need_by_date=payload.get("need_by_date", ""),
        item_code=payload.get("item_code"),
        item_description=payload.get("item_description", ""),
        quantity=float(payload.get("quantity") or 0),
        uom=payload.get("uom", ""),
        purpose=payload.get("purpose", ""),
        priority=payload.get("priority", "NORMAL"),
        budget_code=payload.get("budget_code", ""),
        evidence=payload.get("evidence"),
        raw_message=raw_message,
        status=states.DRAFT,
    )
    if not mr.item_code:
        # AI / manual intake found no catalog match: never invent a code.
        mr.unmapped_item = True
    db.add(mr)
    db.flush()
    audit.record(
        db, actor=str(requester_id), event="request.created",
        entity_type="material_request", entity_id=mr.id,
        after={"request_no": mr.request_no, "status": mr.status},
        channel=channel,
    )
    return mr


def get_or_404(db: Session, mr_id: int) -> MaterialRequest:
    mr = db.get(MaterialRequest, mr_id)
    if mr is None:
        raise NotFound(f"طلب المادة {mr_id} غير موجود")
    return mr


def submit(db: Session, mr_id: int, actor: str) -> MaterialRequest:
    """Requester submits the draft. Missing mandatory fields → NEEDS_INFORMATION."""
    mr = get_or_404(db, mr_id)
    missing = mr.missing_fields()
    if missing:
        _change_status(db, mr, states.NEEDS_INFORMATION, actor)
        db.commit()
        raise DomainError(
            f"حقول إلزامية ناقصة ({', '.join(missing)}) — الطلب بحاجة لاستكمال"
        )
    _change_status(db, mr, states.TECHNICALLY_VALIDATED, actor)
    db.commit()
    return mr


def validate_technical(db: Session, mr_id: int, actor: str) -> MaterialRequest:
    """Technical validation. Flags duplicates in the recent window."""
    mr = get_or_404(db, mr_id)
    if mr.status != states.TECHNICALLY_VALIDATED:
        raise DomainError("التحقق الفني يبدأ من حالة TECHNICALLY_VALIDATED")

    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.duplicate_window_days)
    duplicates = (
        db.query(MaterialRequest)
        .filter(
            MaterialRequest.item_description == mr.item_description,
            MaterialRequest.quantity == mr.quantity,
            MaterialRequest.uom == mr.uom,
            MaterialRequest.id != mr.id,
            MaterialRequest.status.in_([states.DRAFT, states.NEEDS_INFORMATION,
                                       states.SOURCING, states.QUOTATIONS_RECEIVED]),
            MaterialRequest.created_at >= cutoff,
        )
        .all()
    )
    if duplicates:
        raise DomainError(
            "طلب مشابه موجود خلال الفترة القصيرة ("
            + ", ".join(d.request_no for d in duplicates)
            + ") — يرجى تأكيد عدم التكرار"
        )
    _change_status(db, mr, states.SOURCING, actor)
    db.commit()
    return mr


def send_back_for_info(db: Session, mr_id: int, actor: str, note: str) -> MaterialRequest:
    mr = get_or_404(db, mr_id)
    if mr.status not in (states.DRAFT, states.TECHNICALLY_VALIDATED, states.SOURCING):
        raise DomainError("لا يمكن طلب استكمال معلومات في هذه الحالة")
    _change_status(db, mr, states.NEEDS_INFORMATION, actor)
    audit.record(
        db, actor=actor, event="info.requested", entity_type="material_request",
        entity_id=mr.id, after={"note": note},
    )
    db.commit()
    return mr
