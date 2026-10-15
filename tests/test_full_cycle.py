"""End-to-end happy path: request → quotations → approval → PO → payment →
GRN → invoice → 3-way match → ERP export. Mirrors the blueprint cycle."""
from __future__ import annotations

from fastapi.testclient import TestClient

QUOTATION_LINES_A = [
    {"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
     "quantity": 120, "uom": "Sheet", "unit_price": 100.0},
]
QUOTATION_LINES_B = [
    {"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
     "quantity": 120, "uom": "Sheet", "unit_price": 92.0},
]


def _create_mr(client: TestClient) -> dict:
    res = client.post(
        "/api/v1/material-requests",
        json={
            "item_code": "GB-125",
            "item_description": "Gypsum Board MR 12.5mm",
            "quantity": 120,
            "uom": "Sheet",
            "need_by_date": "2026-10-20",
            "purpose": "إغلاق الجدران الداخلية",
            "priority": "NORMAL",
            "budget_code": "PRJ-001/H-07",
        },
        headers={"X-User": "eng1"},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _run_to_pending_gm(client: TestClient, mr_id: int) -> dict:
    # submit + technical validation
    assert client.post(f"/api/v1/material-requests/{mr_id}/submit",
                       headers={"X-User": "eng1"}).status_code == 200
    assert client.post(f"/api/v1/material-requests/{mr_id}/validate-technical",
                       headers={"X-User": "proc1"}).status_code == 200

    # sourcing case + two supplier quotations
    case = client.post("/api/v1/sourcing-cases",
                       json={"material_request_ids": [mr_id]},
                       headers={"X-User": "proc1"})
    assert case.status_code == 200, case.text
    case_id = case.json()["id"]

    for supplier, lines, shipping in (
        ("مورد أ", QUOTATION_LINES_A, 200.0),
        ("مورد ب", QUOTATION_LINES_B, 350.0),
    ):
        res = client.post(
            f"/api/v1/quotations/extract?case_id={case_id}",
            json={"supplier": supplier, "lines": lines,
                  "shipping_cost": shipping, "delivery_days": 2},
            headers={"X-User": "proc1"},
        )
        assert res.status_code == 200, res.text

    # comparison + submit for GM approval
    cmp_res = client.get(f"/api/v1/sourcing-cases/{case_id}/compare",
                         headers={"X-User": "gm1"})
    assert cmp_res.status_code == 200
    quotes = cmp_res.json()["quotations"]
    assert len(quotes) == 2
    assert quotes[0]["comparative_cost"] <= quotes[1]["comparative_cost"]

    sub = client.post(f"/api/v1/sourcing-cases/{case_id}/submit-for-approval",
                     headers={"X-User": "proc1"})
    assert sub.status_code == 200, sub.text
    return {"case_id": case_id, "quotes": quotes}


def test_full_cycle(client: TestClient):
    mr = _create_mr(client)
    mr_id = mr["id"]
    ctx = _run_to_pending_gm(client, mr_id)

    # --- GM approves the cheapest quotation (version 1) ----------------------
    chosen = ctx["quotes"][0]
    dec = client.post(
        f"/api/v1/approvals/{mr_id}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED",
              "comment": "أفضل سعر ومدة توريد"},
        headers={"X-User": "gm1"},
    )
    assert dec.status_code == 200, dec.text
    body = dec.json()
    assert body["snapshot_hash"] == chosen["snapshot_hash"]
    assert body["amount"] == chosen["comparative_cost"]

    mr_now = client.get(f"/api/v1/material-requests/{mr_id}",
                        headers={"X-User": "gm1"}).json()
    assert mr_now["status"] == "APPROVED"

    # --- PO issuance (idempotent) -------------------------------------------
    po_headers = {"X-User": "proc1", "Idempotency-Key": "po-key-1"}
    po1 = client.post(f"/api/v1/purchase-orders?mr_id={mr_id}", headers=po_headers)
    assert po1.status_code == 201, po1.text
    po = po1.json()
    assert po["status"] == "ISSUED"

    # replay with same key + same payload → no duplicate PO
    po2 = client.post(f"/api/v1/purchase-orders?mr_id={mr_id}", headers=po_headers)
    assert po2.status_code == 201
    assert po2.json().get("replayed") is True

    # --- payment request (advance) -------------------------------------------
    pay = client.post(
        "/api/v1/payment-requests",
        json={"purchase_order_id": po["id"], "amount": 1000.0, "purpose": "advance"},
        headers={"X-User": "proc1", "Idempotency-Key": "pay-key-1"},
    )
    assert pay.status_code == 201, pay.text
    pay_id = pay.json()["id"]

    # requester-of-payment cannot approve it (proc1 is requester → gm1 approves)
    denied = client.post(f"/api/v1/payment-requests/{pay_id}/approve",
                        headers={"X-User": "proc1"})
    assert denied.status_code == 403

    ok = client.post(f"/api/v1/payment-requests/{pay_id}/approve",
                     headers={"X-User": "gm1"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "APPROVED"

    # bank execution by accountant; receipt image alone is not an approval
    exec1 = client.post(
        f"/api/v1/payment-requests/{pay_id}/execute?bank_ref=TRF-99881",
        headers={"X-User": "acc1"},
    )
    assert exec1.status_code == 200
    assert exec1.json()["status"] == "PAID"

    # --- goods receipt (full, accepted) --------------------------------------
    grn = client.post(
        "/api/v1/receipts",
        json={
            "purchase_order_id": po["id"],
            "lines": [{
                "item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                "qty_required": 120, "qty_supplied": 120,
                "qty_accepted": 120, "qty_rejected": 0, "condition": "OK",
            }],
            "site": "مصنع الرياض",
        },
        headers={"X-User": "wh1"},
    )
    assert grn.status_code == 200, grn.text
    assert grn.json()["inspection_result"] == "ACCEPTED"

    # --- vendor invoice + 3-way match ---------------------------------------
    inv = client.post(
        "/api/v1/vendor-invoices/extract",
        json={
            "purchase_order_id": po["id"], "supplier": po["supplier"],
            "invoice_no": "INV-77321", "invoice_date": "2026-10-18",
            "lines": [{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                       "quantity": 120, "uom": "Sheet", "unit_price": 92.0}],
            "tax_amount": 1656.0, "charges": 0.0, "total": 12696.0,
        },
        headers={"X-User": "acc1"},
    )
    assert inv.status_code == 200, inv.text
    invoice_id = inv.json()["id"]

    match = client.post(f"/api/v1/vendor-invoices/{invoice_id}/match",
                        headers={"X-User": "acc1"})
    assert match.status_code == 200, match.text
    assert match.json()["match_result"]["type"] == "MATCHED", match.text

    # --- ERP export: EXPORTED is not POSTED ---------------------------------
    export = client.post(f"/api/v1/vendor-invoices/{invoice_id}/post-to-erp",
                        headers={"X-User": "acc1"})
    assert export.status_code == 200, export.text
    assert export.json()["status"] == "EXPORTED"
    assert "bundle" in export.json()

    mr_final = client.get(f"/api/v1/material-requests/{mr_id}",
                          headers={"X-User": "gm1"}).json()
    assert mr_final["status"] == "READY_TO_POST"

    # --- posting only via authenticated ERP confirmation webhook ---------------
    from app.platform.config import get_settings

    settings = get_settings()
    settings.erp_webhook_secret = "erp-secret"
    try:
        # anonymous confirmation is refused
        anon = client.post(
            "/webhooks/erp/export",
            json={"event": "posted", "internal_entity": "vendor_invoice",
                  "internal_id": invoice_id, "external_id": "X"},
        )
        assert anon.json()["ok"] is False

        posted = client.post(
            "/webhooks/erp/export",
            json={"event": "posted", "internal_entity": "vendor_invoice",
                  "internal_id": invoice_id, "external_id": "BILL/2026/0091"},
            headers={"X-ERP-Webhook-Secret": "erp-secret"},
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["status"] == "POSTED"
    finally:
        settings.erp_webhook_secret = ""

    mr_posted = client.get(f"/api/v1/material-requests/{mr_id}",
                           headers={"X-User": "gm1"}).json()
    assert mr_posted["status"] == "POSTED_TO_ERP"

    # --- audit trail: the auditor traces everything back to the request ------
    timeline = client.get(f"/api/v1/material-requests/{mr_id}/timeline",
                          headers={"X-User": "aud1"}).json()
    events = [e["event"] for e in timeline]
    assert "request.created" in events
    assert "approval.decided" in events

    po_events = client.get(f"/api/v1/audit/purchase_order/{po['id']}",
                            headers={"X-User": "aud1"}).json()
    assert any(e["event"] == "po.issued" for e in po_events)

    inv_events = client.get(f"/api/v1/audit/vendor_invoice/{invoice_id}",
                             headers={"X-User": "aud1"}).json()
    assert any(e["event"] == "invoice.matched" for e in inv_events)
    assert any(e["event"] == "invoice.posted_to_erp" for e in inv_events)

    # the decision record binds the exact quotation version and hash
    approval_events = client.get(f"/api/v1/audit/material_request/{mr_id}",
                                  headers={"X-User": "aud1"}).json()
    decision = [e for e in approval_events if e["event"] == "approval.decided"][0]
    assert decision["after"]["snapshot_hash"] == chosen["snapshot_hash"]
    assert decision["after"]["quotation_version"] == chosen["version"]
    assert decision["after"]["amount"] == chosen["comparative_cost"]


def test_intake_message_creates_draft(client: TestClient):
    res = client.post(
        "/api/v1/intake/messages",
        json={"channel": "api", "sender": "eng1",
              "text": "مشروع النور، البيت H-07، نحتاج 120 لوح جبس قبل الأحد"},
        headers={"X-User": "eng1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["classification"] == "material_request"
    assert body["draft_request_no"].startswith("MR-")

    # the draft keeps the raw message and never invents an item code
    drafts = client.get("/api/v1/material-requests", headers={"X-User": "eng1"}).json()
    draft = [d for d in drafts if d["request_no"] == body["draft_request_no"]][0]
    assert draft["status"] == "DRAFT"
    assert draft["item_code"] is None  # unmapped — procurement must map it


def test_whatsapp_webhook_rejects_bad_signature(client: TestClient):
    from app.platform.config import get_settings

    settings = get_settings()
    old_secret = settings.whatsapp_webhook_secret
    settings.whatsapp_webhook_secret = "topsecret"
    try:
        res = client.post("/webhooks/whatsapp", json={"entry": []})
        assert res.status_code == 200
        assert res.json()["ok"] is False
    finally:
        settings.whatsapp_webhook_secret = old_secret


def test_whatsapp_unknown_sender_is_parked(client: TestClient):
    res = client.post(
        "/webhooks/whatsapp",
        json={"entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "+966555555555"}],
            "messages": [{"id": "wamid.1", "type": "text",
                          "text": {"body": "نحتاج براغي"}}]}}]}]},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True  # parked for review, no draft fabricated
