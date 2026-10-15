from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.approvals import service
from app.platform.database import get_db
from app.platform.security import Role, require_roles

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class Decision(BaseModel):
    quotation_id: int
    decision: str                     # APPROVED | REJECTED | MODIFY
    comment: str = ""
    exception_reason: str | None = None


@router.post("/{mr_id}/decision")
def decide(mr_id: int, body: Decision, db: Session = Depends(get_db),
           user=Depends(require_roles(Role.GM, Role.FINANCE))):
    approval = service.decide(
        db, actor=user, mr_id=mr_id,
        quotation_id=body.quotation_id, decision=body.decision,
        comment=body.comment, exception_reason=body.exception_reason,
    )
    return {
        "id": approval.id, "decision": approval.decision,
        "quotation_id": approval.quotation_id,
        "quotation_version": approval.quotation_version,
        "snapshot_hash": approval.snapshot_hash,
        "amount": approval.amount, "currency": approval.currency,
        "supplier": approval.supplier, "decided_at": approval.decided_at.isoformat(),
    }
