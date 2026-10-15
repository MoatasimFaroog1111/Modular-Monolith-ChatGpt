import pytest

from tests.test_manufacturing import _make_unit
from tests.test_manufacturing_cycle import api


def expense_body(uid, ref="EQ-001", category="EQUIPMENT", **kw):
    return {"unit_id": uid, "source_reference": ref, "category": category,
            "description": "إيجار معدات الوحدة", "incurred_date": "2026-10-15",
            "net_amount": "100.01", "nonrecoverable_tax": "5.00", **kw}


def test_only_approved_expenses_and_equipment_enter_cost(client):
    uid = _make_unit(client)["id"]
    path = f"/production-units/{uid}/cost-summary"
    first = api(client, "POST", "/unit-expenses", "fin1", 201, json=expense_body(uid))
    assert api(client, "GET", path, "gm1")["actual"]["equipment"] == 0
    api(client, "POST", f"/unit-expenses/{first['id']}/approve", "fin1", 403,
        json={"note": "لا يجوز اعتماد الذات"})
    api(client, "POST", f"/unit-expenses/{first['id']}/approve", "gm1",
        json={"note": "اعتماد الإيجار والضريبة غير المستردة"})
    other = api(client, "POST", "/unit-expenses", "acc1", 201,
                json=expense_body(uid, "DIRECT-1", "DIRECT", net_amount="50", nonrecoverable_tax="0"))
    api(client, "POST", f"/unit-expenses/{other['id']}/approve", "fin1",
        json={"note": "اعتماد المصروف المباشر"})
    result = api(client, "GET", path, "aud1")
    assert result["actual"]["equipment"] == 105.01
    assert result["actual"]["direct_expenses"] == 50
    assert result["actual"]["total"] == 155.01
    assert len(result["expense_sources"]) == 2
    api(client, "POST", "/unit-expenses", "acc1", 409, json=expense_body(uid, " eq-001 "))
    api(client, "POST", f"/unit-expenses/{first['id']}/approve", "gm1", 409,
        json={"note": "اعتماد مكرر"})
    api(client, "POST", f"/unit-expenses/{first['id']}/cancel", "gm1", 409,
        json={"note": "لا يلغى القيد المعتمد بلا عكس"})


def test_cancel_draft_keeps_audit_without_cost(client):
    uid = _make_unit(client)["id"]
    rec = api(client, "POST", "/unit-expenses", "acc1", 201, json=expense_body(uid))
    api(client, "POST", f"/unit-expenses/{rec['id']}/cancel", "acc1",
        json={"note": "المستند غير صحيح؛ ألغيت المسودة"})
    assert api(client, "GET", "/unit-expenses", "aud1")[0]["status"] == "CANCELLED"
    assert api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")["actual"]["total"] == 0


@pytest.mark.parametrize("changes", [
    {"net_amount": "-1"}, {"net_amount": "0"}, {"net_amount": "NaN"},
    {"nonrecoverable_tax": "-1"}, {"net_amount": "1.001"},
    {"currency": "USD"}, {"incurred_date": "2026-02-30"},
])
def test_invalid_expense_is_rejected(client, changes):
    uid = _make_unit(client)["id"]
    api(client, "POST", "/unit-expenses", "acc1", 422, json=expense_body(uid, **changes))


@pytest.mark.parametrize("actor", ["eng1", "wh1", "proc1", "aud1"])
def test_expense_creation_role_guards(client, actor):
    api(client, "POST", "/unit-expenses", actor, 403, json=expense_body(1))
