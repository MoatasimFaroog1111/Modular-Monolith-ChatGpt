from decimal import Decimal

import pytest

from tests.test_manufacturing import _make_unit, _create_mo
from tests.test_manufacturing_cycle import api, issue, purchase_and_receive


def invoice(client, po, qty=120, price=100, number="SET-1"):
    result = api(client, "POST", "/vendor-invoices/extract", "acc1", json={
        "purchase_order_id": po["id"], "supplier": po["supplier"],
        "invoice_no": number, "invoice_date": "2026-10-15",
        "lines": [{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                   "quantity": qty, "uom": "Sheet", "unit_price": price}],
        "tax_amount": round(qty * price * .15, 2),
        "charges": 0, "total": round(qty * price * 1.15, 2),
    })
    match = api(client, "POST", f"/vendor-invoices/{result['id']}/match", "acc1")
    if match["invoice_status"] == "EXCEPTION":
        api(client, "POST", f"/vendor-invoices/{result['id']}/approve-exception", "gm1",
            json={"justification": "اعتماد فرق السعر بعد مراجعة مستند المورد"})
    return result


def prepared(client, consumed=100):
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    _, _, po, _ = purchase_and_receive(client, uid)
    assert issue(client, uid, po["id"], consumed).status_code == 200
    return uid, po


def approve(client, inv):
    rec = api(client, "POST", "/material-settlements", "acc1", 201,
              json={"invoice_id": inv["id"]})
    api(client, "POST", f"/material-settlements/{rec['id']}/approve", "gm1",
        json={"note": "اعتماد تسوية سعر الفاتورة"})
    return rec


def test_full_variance_consumed_and_inventory_no_double_count(client):
    uid, po = prepared(client)
    inv = invoice(client, po)
    rec = api(client, "POST", "/material-settlements", "fin1", 201,
              json={"invoice_id": inv["id"]})
    path = f"/production-units/{uid}/cost-summary"
    assert api(client, "GET", path, "gm1")["actual"]["materials"] == 9200
    api(client, "POST", f"/material-settlements/{rec['id']}/approve", "fin1", 403,
        json={"note": "لا يسمح باعتماد الذات"})
    api(client, "POST", f"/material-settlements/{rec['id']}/approve", "gm1",
        json={"note": "اعتماد المراجعة المالية"})
    data = api(client, "GET", path, "gm1")
    assert data["actual"]["materials"] == 10000
    assert data["material_variance"] == 800
    assert data["provisional_materials"] == 9200
    assert data["valuation"] == "settled_invoice_price"
    source = data["material_settlement_sources"][0]
    assert source["total_variance_cents"] == 96000
    assert source["inventory_variance_cents"] == 16000
    api(client, "POST", "/material-settlements", "acc1", 409, json={"invoice_id": inv["id"]})
    api(client, "POST", f"/material-settlements/{rec['id']}/approve", "gm1", 409,
        json={"note": "ضغط مكرر لا يكرر القيد"})
    assert api(client, "GET", path, "aud1")["actual"]["materials"] == 10000
    assert issue(client, uid, po["id"], 10).status_code == 200
    data = api(client, "GET", path, "gm1")
    assert data["actual"]["materials"] == 11000
    assert data["material_settlement_sources"][0]["inventory_variance_cents"] == 8000


def test_partial_invoices_and_negative_variance(client):
    uid, po = prepared(client)
    approve(client, invoice(client, po, 60, 90, "PART-A"))
    data = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    assert data["actual"]["materials"] == 9100
    assert data["valuation"] == "provisional_po_price"
    approve(client, invoice(client, po, 60, 100, "PART-B"))
    data = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    assert data["actual"]["materials"] == 9500
    assert data["valuation"] == "settled_invoice_price"
    assert data["material_settlement_sources"][0]["inventory_variance_cents"] == 6000


@pytest.mark.parametrize("total", [1, -1, 10001, -10001, 0])
def test_signed_allocation_conserves_every_cent(total):
    from app.modules.manufacturing.settlements import allocate_cents
    values = allocate_cents(total, {"u1": Decimal(1), "u2": Decimal(1), "inventory": Decimal(1)})
    assert sum(values.values()) == total
    assert max(values.values()) - min(values.values()) <= 1


@pytest.mark.parametrize("actor", ["eng1", "wh1", "aud1", "proc1"])
def test_settlement_role_denied(client, actor):
    api(client, "POST", "/material-settlements", actor, 403, json={"invoice_id": 1})


def test_overlapping_drafts_are_revalidated_on_approval(client):
    uid, po = prepared(client)
    first = invoice(client, po, 120, 100, "FULL")
    second = invoice(client, po, 1, 100, "EXCESS")
    a = api(client, "POST", "/material-settlements", "acc1", 201,
            json={"invoice_id": first["id"]})
    b = api(client, "POST", "/material-settlements", "acc1", 201,
            json={"invoice_id": second["id"]})
    api(client, "POST", f"/material-settlements/{a['id']}/approve", "gm1",
        json={"note": "اعتماد الكمية المستلمة"})
    api(client, "POST", f"/material-settlements/{b['id']}/approve", "gm1", 400,
        json={"note": "لا يجوز تجاوز المقبول رغم اعتماد استثناء الفاتورة"})
    assert api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")["actual"]["materials"] == 10000


def test_changed_invoice_is_not_silently_approved(client):
    from app.platform import database
    from app.modules.accounts_payable.models import VendorInvoice
    uid, po = prepared(client)
    inv = invoice(client, po)
    rec = api(client, "POST", "/material-settlements", "acc1", 201,
              json={"invoice_id": inv["id"]})
    with database.SessionLocal() as db:
        source = db.get(VendorInvoice, inv["id"])
        source.lines = [{**source.lines[0], "unit_price": 101}]
        db.commit()
    api(client, "POST", f"/material-settlements/{rec['id']}/approve", "gm1", 409,
        json={"note": "مصدر تغير بعد إعداد المسودة"})
    assert api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")["actual"]["materials"] == 9200


@pytest.mark.parametrize("field,value", [("quantity", 0), ("quantity", "NaN"), ("unit_price", -1)])
def test_invalid_invoice_numbers_are_rejected_at_intake(client, field, value):
    api(client, "POST", "/vendor-invoices/extract", "acc1", 422, json={
        "purchase_order_id": 1, "supplier": "supplier", "invoice_no": "BAD",
        "lines": [{"quantity": 1, "unit_price": 1, field: value}],
        "total": 1,
    })
