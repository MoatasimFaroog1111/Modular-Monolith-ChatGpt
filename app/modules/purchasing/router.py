from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.purchasing import service
from app.platform.database import get_db
from app.platform.security import Role, require_roles
from app.shared.idempotency import begin_idempotent, finish_idempotent

router = APIRouter(prefix="/api/v1", tags=["purchasing"])


class PaymentIn(BaseModel):
    purchase_order_id: int
    amount: float
    purpose: str = "advance"     # advance | final | partial
    method: str = "bank"         # bank | custody | card
    beneficiary: str = ""


async def _idempotent_wrap(request: Request, db: Session, fn) -> JSONResponse:
    """Financial commands carry Idempotency-Key: replay stored response on
    retry, reject the same key with a different payload."""
    outcome = await begin_idempotent(db, request)
    if outcome is None:                      # no key — plain execution
        body = fn()
        db.commit()
        return JSONResponse(status_code=201, content=body)
    rec, is_replay = outcome
    if is_replay:
        return JSONResponse(
            status_code=rec.status_code,
            content={"replayed": True, "stored_response": rec.response_body},
            headers={"X-Idempotent-Replay": "true"},
        )
    body = fn()
    finish_idempotent(db, rec, 201, json.dumps(body, ensure_ascii=False))
    db.commit()
    return JSONResponse(status_code=201, content=body)


@router.post("/purchase-orders")
async def issue_po(
    request: Request, mr_id: int, db: Session = Depends(get_db),
    user=Depends(require_roles(Role.PROCUREMENT, Role.GM)),
):
    return await _idempotent_wrap(request, db, lambda: _po_body(service.issue_po(db, mr_id=mr_id, actor=user)))


def _po_body(po):
    return {"id": po.id, "po_no": po.po_no, "supplier": po.supplier,
            "total": po.total(), "status": po.status,
            "material_request_id": po.material_request_id}


@router.get("/purchase-orders/{po_id}")
def get_po(po_id: int, db: Session = Depends(get_db), user=Depends(require_roles(
        Role.PROCUREMENT, Role.GM, Role.WAREHOUSE, Role.ACCOUNTANT, Role.FINANCE, Role.AUDITOR))):
    from app.modules.purchasing.models import PurchaseOrder
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        return JSONResponse(status_code=404, content={"detail": "أمر الشراء غير موجود"})
    return _po_body(po)


@router.post("/payment-requests")
async def create_payment(
    body: PaymentIn, request: Request, db: Session = Depends(get_db),
    user=Depends(require_roles(Role.PROCUREMENT, Role.ACCOUNTANT, Role.FINANCE)),
):
    return await _idempotent_wrap(
        request, db,
        lambda: _pay_body(service.create_payment_request(
            db, po_id=body.purchase_order_id, actor=user, amount=body.amount,
            purpose=body.purpose, method=body.method, beneficiary=body.beneficiary,
        )),
    )


def _pay_body(pr):
    return {"id": pr.id, "pay_no": pr.pay_no, "status": pr.status, "amount": pr.amount}


@router.post("/payment-requests/{pay_id}/approve")
def approve(pay_id: int, db: Session = Depends(get_db),
            user=Depends(require_roles(Role.GM, Role.FINANCE))):
    pr = service.approve_payment(db, pay_id=pay_id, actor=user)
    return _pay_body(pr)


@router.post("/payment-requests/{pay_id}/execute")
def execute(pay_id: int, bank_ref: str, outcome: str = "PAID",
            db: Session = Depends(get_db),
            user=Depends(require_roles(Role.ACCOUNTANT))):
    pr = service.record_bank_execution(db, pay_id=pay_id, actor=user,
                                       bank_ref=bank_ref, outcome=outcome)
    return _pay_body(pr)


@router.post("/payment-requests/{pay_id}/reconcile")
def reconcile(pay_id: int, db: Session = Depends(get_db),
              user=Depends(require_roles(Role.ACCOUNTANT))):
    pr = service.reconcile_payment(db, pay_id=pay_id, actor=user)
    return _pay_body(pr)
