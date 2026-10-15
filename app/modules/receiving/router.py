from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.receiving import service
from app.platform.database import get_db
from app.platform.security import Role, require_roles

router = APIRouter(prefix="/api/v1", tags=["receiving"])


class GRNLine(BaseModel):
    item_code: str | None = None
    description: str = ""
    qty_required: float
    qty_supplied: float
    qty_accepted: float
    qty_rejected: float
    condition: str = "OK"
    batch_no: str = ""
    notes: str = ""


class CreateGRN(BaseModel):
    purchase_order_id: int
    lines: list[GRNLine]
    site: str = ""
    photos: list[str] = []


@router.post("/receipts")
def create_receipt(body: CreateGRN, db: Session = Depends(get_db),
                   user=Depends(require_roles(Role.WAREHOUSE, Role.ENGINEER))):
    grn = service.create_grn(
        db, po_id=body.purchase_order_id, actor=user,
        lines=[l.model_dump() for l in body.lines],
        site=body.site, photos=body.photos,
    )
    return {
        "id": grn.id, "grn_no": grn.grn_no,
        "inspection_result": grn.inspection_result,
        "supplier": grn.supplier, "site": grn.site,
    }


@router.get("/purchase-orders/{po_id}/accepted-qty")
def accepted_qty(po_id: int, db: Session = Depends(get_db),
                 user=Depends(require_roles(Role.WAREHOUSE, Role.ACCOUNTANT,
                                           Role.AUDITOR, Role.ENGINEER))):
    return service.accepted_qty_by_item(po_id, db)
