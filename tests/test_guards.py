"""Control tests: separation of duties, state-jump prevention, matching
exceptions, idempotency conflicts, and duplicate invoices."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_full_cycle import _create_mr, _run_to_pending_gm


def test_engineer_cannot_approve_own_request(client: TestClient):
    """SoD: the requester never approves — even if the requester is the GM."""
    from app.platform.config import get_settings
    from app.platform.database import SessionLocal
    from app.platform.security import Role, User
    from app.modules.material_requests import service as mr_service
    from app.modules.approvals import service as approval_service
    from app.shared.errors import PermissionDenied

    with SessionLocal() as db:
        gm = db.query(User).filter(User.username == "gm1").first()
        # simulate the GM being the requester of a pending request
        mr = mr_service.create_request(
            db, requester_id=gm.id,
            payload={"item_description": "براغي", "quantity": 10, "uom": "BOX",
                    "need_by_date": "2026-10-20"},
        )
        db.commit()
        from app.workflow import states as wf
        mr.status = wf.PENDING_GM_APPROVAL
        db.commit()

        from app.modules.sourcing.models import Quotation
        q = Quotation(sourcing_case_id=1, supplier="مورد",
                      lines=[{"quantity": 1, "unit_price": 10}])
        db.add(q)
        db.commit()
        try:
            approval_service.decide(db, actor=gm, mr_id=mr.id,
                                    quotation_id=q.id, decision="APPROVED")
            assert False, "should have raised"
        except PermissionDenied:
            pass


def test_procurement_cannot_approve(client: TestClient):
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    res = client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "proc1"},
    )
    assert res.status_code == 403


def test_no_po_without_approval(client: TestClient):
    mr = _create_mr(client)
    _run_to_pending_gm(client, mr["id"])
    res = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                      headers={"X-User": "proc1"})
    assert res.status_code == 409  # APPROVED required first


def test_state_jump_prevented(client: TestClient):
    """QUOTATIONS_RECEIVED → cannot receive/issue anything (documented rule)."""
    mr = _create_mr(client)
    mr_id = mr["id"]
    # walk to SOURCING first
    assert client.post(f"/api/v1/material-requests/{mr_id}/submit",
                       headers={"X-User": "eng1"}).status_code == 200
    assert client.post(f"/api/v1/material-requests/{mr_id}/validate-technical",
                       headers={"X-User": "proc1"}).status_code == 200

    case = client.post("/api/v1/sourcing-cases",
                      json={"material_request_ids": [mr_id]},
                      headers={"X-User": "proc1"}).json()
    client.post(
        f"/api/v1/quotations/extract?case_id={case['id']}",
        json={"supplier": "مورد أ",
              "lines": [{"quantity": 10, "unit_price": 5}], "delivery_days": 1},
        headers={"X-User": "proc1"},
    )
    # the request moved to QUOTATIONS_RECEIVED — a second submit is a jump
    res = client.post(f"/api/v1/material-requests/{mr_id}/submit",
                     headers={"X-User": "eng1"})
    assert res.status_code == 409

    # and a PO cannot be issued without GM approval
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr_id}",
                     headers={"X-User": "proc1"})
    assert po.status_code == 409


def test_quotation_change_after_approval_invalidates(client: TestClient):
    """GM approved v1; supplier sends v2 → the approval no longer binds."""
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]

    dec = client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    assert dec.status_code == 200

    # supplier sends a new version of the same quotation (higher price)
    res = client.post(
        f"/api/v1/quotations/extract?case_id={ctx['case_id']}",
        json={"supplier": chosen["supplier"],
              "lines": [{"item_code": "GB-125", "description": "Gypsum Board",
                         "quantity": 120, "uom": "Sheet", "unit_price": 150.0}],
              "delivery_days": 3},
        headers={"X-User": "proc1"},
    )
    assert res.status_code == 200
    assert res.json()["version"] == 2

    # PO on the approved (now stale) decision must fail
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                     headers={"X-User": "proc1"})
    assert po.status_code == 409
    assert "تغيّرت بنود العرض" in po.json()["detail"]


def test_idempotency_key_conflict(client: TestClient):
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    headers = {"X-User": "proc1", "Idempotency-Key": "same-key"}
    po1 = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}", headers=headers)
    assert po1.status_code == 201

    # same key, different payload → rejected
    po2 = client.post(f"/api/v1/purchase-orders?mr_id={mr['id'] + 999}",
                      headers=headers)
    assert po2.status_code == 409


def test_duplicate_invoice_rejected(client: TestClient):
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                     headers={"X-User": "proc1"}).json()

    inv_payload = {
        "purchase_order_id": po["id"], "supplier": po["supplier"],
        "invoice_no": "INV-DUP-1",
        "lines": [{"item_code": "GB-125", "quantity": 120,
                   "unit_price": 100.0}],
        "tax_amount": 0.0, "total": 12000.0,
    }
    first = client.post("/api/v1/vendor-invoices/extract", json=inv_payload,
                        headers={"X-User": "acc1"})
    assert first.status_code == 200
    second = client.post("/api/v1/vendor-invoices/extract", json=inv_payload,
                         headers={"X-User": "acc1"})
    assert second.status_code == 409
    assert "مكررة" in second.json()["detail"]


def test_invoice_without_grn_is_missing_receipt(client: TestClient):
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                     headers={"X-User": "proc1"}).json()

    inv = client.post(
        "/api/v1/vendor-invoices/extract",
        json={"purchase_order_id": po["id"], "supplier": po["supplier"],
              "invoice_no": "INV-NOGRN",
              "lines": [{"item_code": "GB-125", "quantity": 120,
                         "unit_price": 100.0}],
              "tax_amount": 0.0, "total": 12000.0},
        headers={"X-User": "acc1"},
    ).json()

    match = client.post(f"/api/v1/vendor-invoices/{inv['id']}/match",
                        headers={"X-User": "acc1"})
    assert match.status_code == 200
    assert match.json()["match_result"]["type"] == "MISSING_RECEIPT"


def test_partial_receipt_full_invoice_is_qty_exception(client: TestClient):
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                     headers={"X-User": "proc1"}).json()

    # accept only 60 of 120
    grn = client.post(
        "/api/v1/receipts",
        json={"purchase_order_id": po["id"],
              "lines": [{"item_code": "GB-125", "description": "Gypsum Board",
                         "qty_required": 120, "qty_supplied": 60,
                         "qty_accepted": 60, "qty_rejected": 0, "condition": "OK"}]},
        headers={"X-User": "wh1"},
    )
    assert grn.status_code == 200
    assert grn.json()["inspection_result"] == "PARTIAL"

    # invoice for the full 120 must fail the match
    inv = client.post(
        "/api/v1/vendor-invoices/extract",
        json={"purchase_order_id": po["id"], "supplier": po["supplier"],
              "invoice_no": "INV-PART",
              "lines": [{"item_code": "GB-125", "quantity": 120,
                         "unit_price": 100.0}],
              "tax_amount": 0.0, "total": 12000.0},
        headers={"X-User": "acc1"},
    ).json()

    match = client.post(f"/api/v1/vendor-invoices/{inv['id']}/match",
                        headers={"X-User": "acc1"})
    result = match.json()["match_result"]
    assert result["type"] == "QTY_EXCEPTION"
    assert result["difference"] == 60


def test_price_exception_and_approved_exception_flow(client: TestClient):
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                     headers={"X-User": "proc1"}).json()
    client.post(
        "/api/v1/receipts",
        json={"purchase_order_id": po["id"],
              "lines": [{"item_code": "GB-125", "description": "Gypsum Board",
                         "qty_required": 120, "qty_supplied": 120,
                         "qty_accepted": 120, "qty_rejected": 0, "condition": "OK"}]},
        headers={"X-User": "wh1"},
    )

    # invoice price 110 vs PO price 100 (10% > tolerance 0%)
    inv = client.post(
        "/api/v1/vendor-invoices/extract",
        json={"purchase_order_id": po["id"], "supplier": po["supplier"],
              "invoice_no": "INV-PRICE",
              "lines": [{"item_code": "GB-125", "quantity": 120,
                         "unit_price": 110.0}],
              "tax_amount": 0.0, "total": 13200.0},
        headers={"X-User": "acc1"},
    ).json()

    match = client.post(f"/api/v1/vendor-invoices/{inv['id']}/match",
                        headers={"X-User": "acc1"})
    assert match.json()["match_result"]["type"] == "PRICE_EXCEPTION"

    # an unapproved exception blocks export — no bypass to the ERP
    blocked = client.post(f"/api/v1/vendor-invoices/{inv['id']}/post-to-erp",
                          headers={"X-User": "acc1"})
    assert blocked.status_code == 400
    assert "استثناء" in blocked.json()["detail"]

    # an accountant cannot approve the exception
    denied = client.post(
        f"/api/v1/vendor-invoices/{inv['id']}/approve-exception",
        json={"justification": "ارتفاع سعر السوق"},
        headers={"X-User": "acc1"},
    )
    assert denied.status_code == 403

    # the GM can, with a documented justification
    approved = client.post(
        f"/api/v1/vendor-invoices/{inv['id']}/approve-exception",
        json={"justification": "قبول فرق السعر بعد موافقة المالك"},
        headers={"X-User": "gm1"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "READY_TO_POST"
