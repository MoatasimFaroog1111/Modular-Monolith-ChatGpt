from decimal import Decimal

from app.modules.manufacturing.landed_costs import _split


def unit(client, name, method="CONTAINER"):
    response = client.post("/api/v1/production-units", headers={"X-User": "eng1"},
                           json={"house_no": name, "build_method": method})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def allocation(client, units, **overrides):
    body = {"source_reference": "SHIP-001", "freight": "100.01", "customs": "50.00",
            "basis": "EQUAL", "lines": [{"unit_id": u} for u in units]}
    body.update(overrides)
    return client.post("/api/v1/landed-costs", json=body, headers={"X-User": "acc1"})


def test_largest_remainder_exact():
    assert _split(100, [Decimal(1)] * 3) == [34, 33, 33]
    assert sum(_split(99999, [Decimal("0.1"), Decimal("3.7"), Decimal("8")])) == 99999


def test_approved_allocation_flows_to_cost_and_audit(client):
    ids = [unit(client, f"H-{i}") for i in range(3)]
    created = allocation(client, ids)
    assert created.status_code == 201, created.text
    body = created.json()
    assert sum(line["freight_cents"] for line in body["lines"]) == 10001
    assert sum(line["customs_cents"] for line in body["lines"]) == 5000
    summary_url = f"/api/v1/production-units/{ids[0]}/cost-summary"
    assert client.get(summary_url, headers={"X-User": "gm1"}).json()["actual"]["landed_cost"] == 0
    approved = client.post(f"/api/v1/landed-costs/{body['id']}/approve",
                           headers={"X-User": "gm1"}, json={"note": "مراجعة بيان الشحن والجمارك"})
    assert approved.status_code == 200, approved.text
    summary = client.get(summary_url, headers={"X-User": "gm1"}).json()
    assert summary["actual"]["landed_cost"] == body["lines"][0]["total"]
    assert summary["actual"]["total"] == body["lines"][0]["total"]
    assert summary["landed_cost_sources"][0]["source_reference"] == "SHIP-001"
    repeat = client.post(f"/api/v1/landed-costs/{body['id']}/approve",
                         headers={"X-User": "gm1"}, json={"note": "إعادة اعتماد"})
    assert repeat.status_code == 409
    events = client.get(f"/api/v1/audit/landed_cost/{body['id']}",
                        headers={"X-User": "aud1"}).json()
    assert [ev["event"] for ev in events] == ["landed_cost.created", "landed_cost.approved"]


def test_weight_value_and_validation(client):
    a, b = unit(client, "C1"), unit(client, "C2")
    response = allocation(client, [a, b], basis="WEIGHT", freight="100", customs="20",
                          lines=[{"unit_id": a, "basis_value": 1},
                                 {"unit_id": b, "basis_value": 3}])
    assert response.status_code == 201
    assert [ln["total"] for ln in response.json()["lines"]] == [30, 90]
    assert allocation(client, [a], source_reference=" ship-001 ").status_code == 409
    assert allocation(client, [a, a], source_reference="DUP").status_code == 400
    assert allocation(client, [a], source_reference="ZERO", freight=0, customs=0).status_code == 400
    assert allocation(client, [a], source_reference="NEG", freight=-1).status_code == 422
    assert allocation(client, [a], source_reference="FX", currency="USD").status_code == 422
    assert allocation(client, [unit(client, "F1", "FRAMECAD")],
                      source_reference="FRAME").status_code == 400
    value = allocation(client, [a, b], source_reference="VALUE", basis="VALUE", freight=20, customs=0,
                       lines=[{"unit_id": a, "basis_value": 100},
                              {"unit_id": b, "basis_value": 300}])
    assert [ln["total"] for ln in value.json()["lines"]] == [5, 15]


def test_allocation_separation_and_read_permissions(client):
    a = unit(client, "C1")
    body = {"source_reference": "FIN-1", "freight": 25, "basis": "EQUAL",
            "lines": [{"unit_id": a}]}
    assert client.post("/api/v1/landed-costs", json=body,
                       headers={"X-User": "eng1"}).status_code == 403
    created = client.post("/api/v1/landed-costs", json=body,
                          headers={"X-User": "fin1"}).json()
    assert client.post(f"/api/v1/landed-costs/{created['id']}/approve",
                       headers={"X-User": "fin1"}, json={"note": "نفس المستخدم"}).status_code == 403
    assert client.get("/api/v1/landed-costs", headers={"X-User": "wh1"}).status_code == 403


def test_foreign_po_values_are_not_added_to_sar_totals(client):
    from app.platform import database
    from app.modules.manufacturing.models import MaterialIssue
    from app.modules.purchasing.models import PurchaseOrder
    a = unit(client, "FX-CONTAINER")
    with database.SessionLocal() as db:
        po = PurchaseOrder(po_no="PO-USD", material_request_id=1, approval_id=1,
                           supplier="foreign", currency="USD", lines=[])
        db.add(po); db.flush()
        db.add(MaterialIssue(issue_no="MI-FX", unit_id=a, issued_by=1,
                             lines=[{"purchase_order_id": po.id, "quantity": 2,
                                     "unit_price": 100, "item_code": "FOREIGN"}]))
        db.commit()
    result = client.get(f"/api/v1/production-units/{a}/cost-summary",
                        headers={"X-User": "gm1"}).json()
    assert result["flags"]["currency_conversion_required"]
    assert result["actual"]["total"] is None
    assert result["actual"]["materials"] is None
    assert result["deviation"]["total"] is None
