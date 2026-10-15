from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.material_requests import service as mr_service
from app.modules.sourcing import service
from app.platform.database import get_db
from app.platform.security import Role, require_roles

router = APIRouter(prefix="/api/v1", tags=["sourcing"])


class CreateCase(BaseModel):
    material_request_ids: list[int]


class QuotationIn(BaseModel):
    supplier: str
    supplier_vat: str = ""
    currency: str = "SAR"
    lines: list[dict]
    shipping_cost: float = 0.0
    extra_costs: float = 0.0
    non_refundable_tax: float = 0.0
    discount: float = 0.0
    delivery_days: int = 0
    payment_terms: str = ""
    valid_until: str = ""


@router.post("/sourcing-cases")
def create_case(body: CreateCase, db: Session = Depends(get_db),
                user=Depends(require_roles(Role.PROCUREMENT))):
    case = service.create_case(db, owner_id=user.id, mr_ids=body.material_request_ids)
    db.commit()
    return {"id": case.id, "material_request_ids": case.material_request_ids, "status": case.status}


@router.post("/quotations/extract")
def add_quotation(body: QuotationIn, case_id: int, db: Session = Depends(get_db),
                  user=Depends(require_roles(Role.PROCUREMENT))):
    """Register an extracted quotation. Extraction (OCR/AI) is an upstream port;
    here we only accept structured data and version it per supplier."""
    q = service.add_quotation(db, case_id=case_id, payload=body.model_dump())
    db.commit()
    return {
        "id": q.id, "supplier": q.supplier, "version": q.version,
        "comparative_cost": q.comparative_cost(),
        "snapshot_hash": service.quotation_snapshot_hash(q),
    }


@router.get("/sourcing-cases/{case_id}/compare")
def compare(case_id: int, db: Session = Depends(get_db),
            user=Depends(require_roles(Role.PROCUREMENT, Role.GM, Role.FINANCE, Role.AUDITOR))):
    return service.compare(db, case_id)


@router.post("/sourcing-cases/{case_id}/submit-for-approval")
def submit_for_approval(case_id: int, db: Session = Depends(get_db),
                        user=Depends(require_roles(Role.PROCUREMENT))):
    return service.mark_pending_gm(db, case_id, user.username)
