from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.modules.accounts_payable import service
from app.platform.database import get_db
from app.platform.security import Role, require_roles

router = APIRouter(prefix="/api/v1", tags=["accounts-payable"])


class InvoiceLineIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    item_code: str | None = None
    description: str = ""
    quantity: float = Field(gt=0, allow_inf_nan=False)
    uom: str = ""
    unit_price: float = Field(ge=0, allow_inf_nan=False)


class InvoiceIn(BaseModel):
    purchase_order_id: int
    supplier: str
    invoice_no: str
    invoice_date: str = ""
    currency: str = "SAR"
    lines: list[InvoiceLineIn] = Field(min_length=1, max_length=500)
    tax_amount: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    charges: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    total: float = Field(default=0.0, ge=0, allow_inf_nan=False)


@router.post("/vendor-invoices/extract")
def register_invoice(body: InvoiceIn, db: Session = Depends(get_db),
                     user=Depends(require_roles(Role.ACCOUNTANT, Role.PROCUREMENT))):
    """Register an extracted invoice. Extraction (OCR/AI) is upstream; here we
    only accept structured data. Same supplier+invoice_no is rejected."""
    inv = service.create_invoice(db, payload=body.model_dump(), actor=user)
    return {"id": inv.id, "invoice_no": inv.invoice_no, "status": inv.status}


@router.post("/vendor-invoices/{invoice_id}/match")
def match(invoice_id: int, db: Session = Depends(get_db),
          user=Depends(require_roles(Role.ACCOUNTANT, Role.FINANCE))):
    return service.match_invoice(db, invoice_id=invoice_id, actor=user)


@router.post("/vendor-invoices/{invoice_id}/approve-exception")
def approve_exception(invoice_id: int, body: dict, db: Session = Depends(get_db),
                      user=Depends(require_roles(Role.GM, Role.FINANCE))):
    return service.approve_exception(
        db, invoice_id=invoice_id, actor=user,
        justification=body.get("justification", ""),
    )


@router.post("/vendor-invoices/{invoice_id}/post-to-erp")
def post_to_erp(invoice_id: int, db: Session = Depends(get_db),
                user=Depends(require_roles(Role.ACCOUNTANT, Role.FINANCE))):
    """Export path: builds the handoff bundle. Status becomes EXPORTED —
    which is deliberately NOT Posted; posting requires real ERP confirmation
    via the integration hub (webhooks/erp)."""
    return service.export_bundle(db, invoice_id=invoice_id, actor=user)


@router.post("/vendor-invoices/{invoice_id}/confirm-posted")
def confirm_posted(invoice_id: int, external_id: str, db: Session = Depends(get_db),
                   user=Depends(require_roles(Role.ACCOUNTANT, Role.FINANCE))):
    return service.mark_posted(db, invoice_id=invoice_id, actor=user.username,
                               external_id=external_id)
