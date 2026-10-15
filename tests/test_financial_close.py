from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_manufacturing import _add_bom, _make_unit, _create_mo, _mos_wos
from tests.test_manufacturing_cycle import api, cycle_client, issue
from tests.test_material_settlements import approve, invoice, prepared
from tests.test_unit_expenses import expense_body
from tests.test_work_centers import center


def complete_unit(c, uid):
    wc = center(c, name="مركز إقفال", hourly_rate="35").json()
    for wo in _mos_wos(c, uid):
        api(c, "PATCH", f"/work-orders/{wo['id']}/plan",
            json={"work_center_id": wc["id"], "estimated_hours": 1})
        api(c, "POST", "/progress-events", json={
            "unit_id": uid, "work_order_id": wo["id"],
            "percent_complete": 100, "hours": 1,
        })


def ready_unit(c, monkeypatch):
    from app.platform.config import get_settings
    monkeypatch.setattr(get_settings(), "overhead_pct", 10)
    uid, po = prepared(c)
    _add_bom(c, uid)
    inv = invoice(c, po)
    approve(c, inv)
    complete_unit(c, uid)
    return uid, po, inv


def close(c, uid, actor="fin1"):
    path = f"/production-units/{uid}/financial-close"
    check = api(c, "GET", path + "/check", "gm1")
    assert check["can_close"], check["blockers"]
    return api(c, "POST", path, actor, json={
        "note": "اكتملت مراجعة المواد والفواتير والمصاريف",
        "expected_summary_hash": check["summary_hash"],
    })


def test_three_stages_end_to_end(cycle_client, monkeypatch):
    c = cycle_client
    uid, po, inv = ready_unit(c, monkeypatch)
    path = f"/production-units/{uid}/financial-close"
    expense = api(c, "POST", "/unit-expenses", "acc1", 201, json=expense_body(uid))
    assert not api(c, "GET", path + "/check", "gm1")["can_close"]
    api(c, "POST", f"/unit-expenses/{expense['id']}/approve", "gm1",
        json={"note": "اعتماد المعدات والضريبة غير المستردة"})
    closed = close(c, uid)
    expected = 10000 + 385 + 1038.5 + 105.01
    assert closed["snapshot"]["actual"]["total"] == round(expected, 2)
    assert closed["snapshot"]["valuation"] == "settled_invoice_price"
    frozen = api(c, "GET", f"/production-units/{uid}/cost-summary", "aud1")
    assert frozen["financial_close"]["snapshot_hash"] == closed["snapshot_hash"]
    for method, url, actor, body in [
        ("POST", "/unit-expenses", "acc1", expense_body(uid, "AFTER-CLOSE")),
        ("POST", "/material-settlements", "acc1", {"invoice_id": inv["id"]}),
        ("POST", f"/material-settlements/1/approve", "gm1", {"note": "إعادة اعتماد"}),
        ("POST", f"/production-units/{uid}/bom", "eng1", [
            {"description": "إضافة بعد الإقفال", "quantity": 1, "estimated_unit_price": 1}]),
        ("POST", f"/production-units/{uid}/manufacturing-orders", "eng1", None),
        ("POST", "/progress-events", "eng1", {"unit_id": uid, "hours": 1}),
        ("PATCH", f"/work-orders/{_mos_wos(c, uid)[0]['id']}/plan", "eng1",
         {"estimated_hours": 2, "work_center_id": None}),
        ("POST", "/landed-costs", "fin1", {
            "source_reference": "CLOSED-SHIP", "basis": "EQUAL", "freight": 1,
            "lines": [{"unit_id": uid}]}),
        ("POST", f"/vendor-invoices/{inv['id']}/match", "acc1", None),
        ("POST", "/receipts", "wh1", {"purchase_order_id": po["id"], "lines": []}),
    ]:
        api(c, method, url, actor, 409, **({"json": body} if body is not None else {}))
    assert issue(c, uid, po["id"], 1).status_code == 409
    api(c, "POST", path + "/reopen", "fin1", 403,
        json={"close_id": closed["id"], "note": "لا يفتح منفذ الإقفال بنفسه"})
    api(c, "POST", path + "/reopen", "gm1", json={
        "close_id": closed["id"], "note": "إعادة فتح لإضافة مستند مصروف متأخر",
    })
    newer = api(c, "POST", "/unit-expenses", "acc1", 201,
                json=expense_body(uid, "LATE-EXP", "DIRECT", net_amount="10", nonrecoverable_tax="0"))
    api(c, "POST", f"/unit-expenses/{newer['id']}/approve", "fin1",
        json={"note": "اعتماد المستند المتأخر"})
    next_close = close(c, uid, "gm1")
    assert next_close["revision"] == 2
    assert next_close["snapshot"]["actual"]["total"] == round(expected + 10, 2)
    history = api(c, "GET", path, "aud1")
    assert len(history) == 2
    assert history[0]["status"] == "REOPENED" and history[1]["status"] == "CLOSED"
    assert history[0]["snapshot"] == closed["snapshot"]
    assert history[0]["snapshot_hash"] == closed["snapshot_hash"]
    events = api(c, "GET", f"/audit/production_unit/{uid}", "aud1")
    assert [e["event"] for e in events].count("unit.financial_closed") == 2
    assert [e["event"] for e in events].count("unit.financial_reopened") == 1


def test_snapshot_frozen_despite_configuration_change(client, monkeypatch):
    uid, _, _ = ready_unit(client, monkeypatch)
    closed = close(client, uid)
    from app.platform.config import get_settings
    monkeypatch.setattr(get_settings(), "overhead_pct", 90)
    data = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    assert data["actual"] == closed["snapshot"]["actual"]
    assert data["estimated"] == closed["snapshot"]["estimated"]


def test_changed_cost_stale_hash_and_repeat_close_rejected(client, monkeypatch):
    uid, _, _ = ready_unit(client, monkeypatch)
    path = f"/production-units/{uid}/financial-close"
    check = api(client, "GET", path + "/check", "gm1")
    rec = api(client, "POST", "/unit-expenses", "acc1", 201, json=expense_body(uid))
    api(client, "POST", f"/unit-expenses/{rec['id']}/approve", "gm1",
        json={"note": "مستند أحدث من مراجعة الإقفال"})
    api(client, "POST", path, "fin1", 409,
        json={"note": "مراجعة قديمة", "expected_summary_hash": check["summary_hash"]})
    closed = close(client, uid)
    api(client, "POST", path, "fin1", 409,
        json={"note": "طلب مكرر", "expected_summary_hash": closed["snapshot_hash"]})
    api(client, "POST", path + "/reopen", "gm1", 409,
        json={"close_id": closed["id"] + 1, "note": "إقفال آخر غير مطابق"})


def test_close_rejects_incomplete_unsettled_and_missing_overhead(client, monkeypatch):
    from app.platform.config import get_settings
    monkeypatch.setattr(get_settings(), "overhead_pct", None)
    uid, po = prepared(client)
    _add_bom(client, uid)
    check = api(client, "GET", f"/production-units/{uid}/financial-close/check", "gm1")
    assert not check["can_close"]
    assert any("الإنتاج" in b for b in check["blockers"])
    assert any("تسو" in b for b in check["blockers"])
    assert any("غير المباشرة" in b for b in check["blockers"])


def test_concurrent_close_creates_one_revision(client, monkeypatch):
    uid, _, _ = ready_unit(client, monkeypatch)
    path = f"/production-units/{uid}/financial-close"
    check = api(client, "GET", path + "/check", "gm1")
    body = {"note": "اختبار الإقفال المتزامن", "expected_summary_hash": check["summary_hash"]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/v1" + path,
            headers={"X-User": "fin1"}, json=body), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(api(client, "GET", path, "aud1")) == 1


def test_closed_unit_does_not_freeze_remaining_stock(client, monkeypatch):
    uid, po, _ = ready_unit(client, monkeypatch)
    closed = close(client, uid)
    other = _make_unit(client)["id"]
    _create_mo(client, other)
    assert issue(client, other, po["id"], 10).status_code == 200
    other_cost = api(client, "GET", f"/production-units/{other}/cost-summary", "gm1")
    assert other_cost["actual"]["materials"] == 1000
    source = other_cost["material_settlement_sources"][0]
    fixed = closed["snapshot"]["material_settlement_sources"][0]["unit_variance_cents"]
    assert fixed + source["unit_variance_cents"] + source["inventory_variance_cents"] == 96000
    current = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    assert current["actual"] == closed["snapshot"]["actual"]


@pytest.mark.parametrize("actor", ["eng1", "wh1", "proc1", "acc1", "aud1"])
def test_financial_close_role_guards(client, actor):
    api(client, "POST", "/production-units/1/financial-close", actor, 403,
        json={"note": "غير مخول", "expected_summary_hash": "a" * 64})


def test_modified_snapshot_is_detected(client, monkeypatch):
    from app.platform import database
    from app.modules.manufacturing.financial_close import FinancialClose
    import copy
    uid, _, _ = ready_unit(client, monkeypatch)
    closed = close(client, uid)
    with database.SessionLocal() as db:
        rec = db.get(FinancialClose, closed["id"])
        changed = copy.deepcopy(rec.snapshot)
        changed["actual"]["total"] += 1
        rec.snapshot = changed
        db.commit()
    api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1", 409)


def test_additional_invoice_must_also_be_settled_before_close(client, monkeypatch):
    uid, po, _ = ready_unit(client, monkeypatch)
    invoice(client, po, 1, 100, "EXTRA-UNSETTLED")
    path = f"/production-units/{uid}/financial-close"
    check = api(client, "GET", path + "/check", "gm1")
    assert not check["can_close"]
    assert "توجد فواتير مصدر لم تعتمد تسويتها" in check["blockers"]
