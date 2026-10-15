from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.material_requests import service
from app.platform.database import get_db
from app.platform.security import Role, current_user, require_roles
from app.shared.errors import DomainError
from app.shared.idempotency import begin_idempotent, finish_idempotent

router = APIRouter(prefix="/api/v1/material-requests", tags=["material-requests"])


class CreateMR(BaseModel):
    project_id: int | None = None
    production_unit_id: int | None = None
    manufacturing_order_id: int | None = None
    need_by_date: str = ""
    item_code: str | None = None
    item_description: str = ""
    quantity: float = 0.0
    uom: str = ""
    purpose: str = ""
    priority: str = "NORMAL"
    budget_code: str = ""
    evidence: dict | None = None


def _serialize(mr):
    return {
        "id": mr.id,
        "request_no": mr.request_no,
        "status": mr.status,
        "requester_id": mr.requester_id,
        "project_id": mr.project_id,
        "production_unit_id": mr.production_unit_id,
        "need_by_date": mr.need_by_date,
        "item_code": mr.item_code,
        "item_description": mr.item_description,
        "unmapped_item": mr.unmapped_item,
        "quantity": mr.quantity,
        "uom": mr.uom,
        "purpose": mr.purpose,
        "priority": mr.priority,
        "budget_code": mr.budget_code,
        "created_at": mr.created_at.isoformat() if mr.created_at else None,
    }


@router.post("")
def create(body: CreateMR, db: Session = Depends(get_db),
            user=Depends(require_roles(Role.ENGINEER, Role.PROCUREMENT))):
    mr = service.create_request(db, requester_id=user.id, payload=body.model_dump())
    db.commit()
    return _serialize(mr)


@router.get("")
def list_requests(db: Session = Depends(get_db), user=Depends(current_user)):
    from app.modules.material_requests.models import MaterialRequest
    rows = db.query(MaterialRequest).order_by(MaterialRequest.id.desc()).limit(100).all()
    return [_serialize(r) for r in rows]


@router.get("/{mr_id}")
def get_one(mr_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    mr = service.get_or_404(db, mr_id)
    return _serialize(mr)


@router.get("/{mr_id}/timeline")
def timeline(mr_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    service.get_or_404(db, mr_id)
    events = audit.timeline(db, "material_request", mr_id)
    return [
        {
            "id": e.id, "actor": e.actor, "event": e.event,
            "before": e.before, "after": e.after,
            "channel": e.channel, "result": e.result,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.post("/{mr_id}/submit")
def submit(mr_id: int, db: Session = Depends(get_db), user=Depends(current_user)):
    mr = service.get_or_404(db, mr_id)
    if mr.requester_id != user.id:
        raise DomainError("فقط مُنشئ الطلب يستطيع إرساله")
    mr = service.submit(db, mr_id, user.username)
    return _serialize(mr)


@router.post("/{mr_id}/validate-technical")
def validate_technical(mr_id: int, db: Session = Depends(get_db),
                        user=Depends(require_roles(Role.ENGINEER, Role.PROCUREMENT))):
    mr = service.validate_technical(db, mr_id, user.username)
    return _serialize(mr)
