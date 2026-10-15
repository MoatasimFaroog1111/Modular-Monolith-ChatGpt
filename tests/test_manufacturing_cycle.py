"""Complete manufacturing/cost integration and adversarial regression cases.

Complete cycles run both in-process and over HTTP to an isolated Uvicorn/SQLite
instance. No preview, production database, or external ERP is modified.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from tests.test_full_cycle import _run_to_pending_gm
from tests.test_manufacturing import _add_bom, _create_mo, _make_unit, _mos_wos
from tests.test_work_centers import center


def api(client, method, path, actor="eng1", expected=200, **kwargs):
    response = client.request(method, f"/api/v1{path}",
                              headers={"X-User": actor}, **kwargs)
    assert response.status_code == expected, response.text
    return response.json()


@pytest.fixture(params=["inprocess", "http"])
def cycle_client(request, client, tmp_path):
    if request.param == "inprocess":
        yield client
        return
    # OS chooses an ephemeral port; process and database are per test.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'live-cycle.db'}",
           "DEV_MODE": "true", "DEFAULT_LABOR_RATE": "0",
           "OVERHEAD_PCT": "10", "ERP_CONNECTOR": "export",
           "PYTHONPATH": str(root)}
    with (tmp_path / "uvicorn.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=root, env=env, stdout=log, stderr=log,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as live:
                for _ in range(150):
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(f"Uvicorn exited: {log.read()}")
                    try:
                        if live.get("/api/v1/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
                else:
                    pytest.fail("Isolated HTTP server did not become ready")
                yield live
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def purchase_and_receive(client, unit_id):
    mr = api(client, "POST", "/material-requests", json={
        "production_unit_id": unit_id, "item_code": "GB-125",
        "item_description": "Gypsum Board MR 12.5mm", "quantity": 120,
        "uom": "Sheet", "need_by_date": "2026-10-20",
        "purpose": "إغلاق الجدران الداخلية", "priority": "NORMAL",
    })
    ctx = _run_to_pending_gm(client, mr["id"])
    approved = api(client, "POST", f"/approvals/{mr['id']}/decision", "gm1",
                   json={"quotation_id": ctx["quotes"][0]["quotation_id"],
                         "decision": "APPROVED"})
    po = api(client, "POST", f"/purchase-orders?mr_id={mr['id']}",
             "proc1", expected=201)
    receipt = api(client, "POST", "/receipts", "wh1", json={
        "purchase_order_id": po["id"], "lines": [{
            "item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
            "qty_required": 120, "qty_supplied": 120,
            "qty_accepted": 120, "qty_rejected": 0, "condition": "OK",
        }],
    })
    return mr, approved, po, receipt


def issue(client, unit_id, po_id, quantity):
    return client.post(f"/api/v1/material-issues?unit_id={unit_id}", json={
        "item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
        "quantity": quantity, "uom": "Sheet", "purchase_order_id": po_id,
    }, headers={"X-User": "wh1"})


@pytest.mark.parametrize("method,stages", [("CONTAINER", 10), ("FRAMECAD", 11)])
def test_complete_cycle(cycle_client, monkeypatch, method, stages):
    from app.platform.config import get_settings

    monkeypatch.setattr(get_settings(), "overhead_pct", 10)
    c = cycle_client
    unit = _make_unit(c, method)
    uid = unit["id"]
    _add_bom(c, uid)  # 100*92 + 20*45 = 10,100 SAR.
    mo = _create_mo(c, uid)
    wos = _mos_wos(c, uid)
    assert len(wos) == stages
    wc = center(c, name=f"QA-{method}", hourly_rate="35").json()
    for wo in wos:
        api(c, "PATCH", f"/work-orders/{wo['id']}/plan",
            json={"work_center_id": wc["id"], "estimated_hours": 2})
    api(c, "POST", f"/work-orders/{wos[1]['id']}/start", expected=400)
    mr, approval, po, grn = purchase_and_receive(c, uid)
    first_issue = issue(c, uid, po["id"], 60)
    assert first_issue.status_code == 200, first_issue.text
    second_issue = issue(c, uid, po["id"], 40)
    assert second_issue.status_code == 200, second_issue.text
    assert first_issue.json()["materials_cost"] == 5520
    assert second_issue.json()["materials_cost"] == 3680
    assert issue(c, uid, po["id"], 21).status_code == 400

    summary_path = f"/production-units/{uid}/cost-summary"
    allocation_total = Decimal("0")
    if method == "CONTAINER":
        other = _make_unit(c, "CONTAINER")["id"]
        cost = api(c, "POST", "/landed-costs", "fin1", 201, json={
            "source_reference": "CYCLE-SHIP", "basis": "WEIGHT",
            "freight": "100.01", "customs": "50.00",
            "lines": [{"unit_id": uid, "basis_value": 1},
                      {"unit_id": other, "basis_value": 3}],
        })
        assert api(c, "GET", summary_path, "gm1")["actual"]["landed_cost"] == 0
        api(c, "POST", f"/landed-costs/{cost['id']}/approve", "fin1", 403,
            json={"note": "رفض اعتماد المستخدم نفسه"})
        api(c, "POST", f"/landed-costs/{cost['id']}/approve", "gm1",
            json={"note": "مراجعة مستند الشحن والجمارك"})
        allocation_total = Decimal("37.50")
        assert sum(line["freight_cents"] for line in cost["lines"]) == 10001
        assert sum(line["customs_cents"] for line in cost["lines"]) == 5000

    first = wos[0]
    api(c, "POST", f"/work-orders/{first['id']}/start")
    partial = api(c, "POST", "/progress-events", json={
        "unit_id": uid, "work_order_id": first["id"], "percent_complete": 50,
        "workers": 4, "hours": 28, "event_date": "2026-10-12",
        "stoppages": [{"hours": 2, "reason": "نقص البراغي"}],
        "material_shortage": True, "shortage_description": "براغي تثبيت ناقصة",
    })
    assert partial["labor_cost"] == 980  # person-hours, not 4 * 28.
    linked = api(c, "GET", f"/material-requests/{partial['linked_material_request_id']}")
    assert linked["production_unit_id"] == uid and linked["priority"] == "STOPPING"
    api(c, "PATCH", f"/work-orders/{first['id']}/plan", expected=409,
        json={"work_center_id": wc["id"], "estimated_hours": 999})
    api(c, "PATCH", f"/work-centers/{wc['id']}", "gm1", json={
        "name": wc["name"], "hourly_rate": 40, "version": 1,
    })
    assert api(c, "GET", summary_path, "gm1")["actual"]["labor"] == 980
    api(c, "POST", "/progress-events", json={
        "unit_id": uid, "work_order_id": first["id"], "percent_complete": 100,
        "workers": 1, "hours": 1, "event_date": "2026-10-13",
    })
    for wo in wos[1:]:
        api(c, "POST", f"/work-orders/{wo['id']}/start")
        api(c, "POST", "/progress-events", json={
            "unit_id": uid, "work_order_id": wo["id"], "percent_complete": 100,
            "workers": 1, "hours": 1, "event_date": "2026-10-14",
        })
    progress = api(c, "GET", f"/production-units/{uid}/progress")
    assert progress["overall_percent"] == 100
    assert all(w["status"] == "DONE" for w in progress["work_orders"])
    units = api(c, "GET", "/production-units")
    assert next(u for u in units if u["id"] == uid)["status"] == "DONE"
    events = api(c, "GET", f"/audit/manufacturing_order/{mo['id']}", "aud1")
    assert [e["event"] for e in events].count("mo.completed") == 1

    actual_labor = Decimal(980 + stages * 40)
    overhead = (Decimal(9200) + actual_labor) / 10
    expected_total = Decimal(9200) + actual_labor + overhead + allocation_total
    summary = api(c, "GET", summary_path, "gm1")
    assert summary["estimated"] == {
        "materials": 10100, "labor": stages * 2 * 40,
        "total": 10100 + stages * 2 * 40,
    }
    assert Decimal(str(summary["actual"]["total"])) == expected_total
    assert summary["actual"]["materials"] == 9200
    assert summary["actual"]["labor"] == float(actual_labor)
    assert summary["actual"]["overhead"] == float(overhead)
    assert summary["actual"]["labor_hours"] == 28 + stages
    assert summary["actual"]["landed_cost"] == float(allocation_total)
    assert summary["deviation"]["total"] == float(expected_total - Decimal(summary["estimated"]["total"]))
    assert summary["flags"]["materials_unsettled"]
    assert not any(v for k, v in summary["flags"].items() if k != "materials_unsettled")
    week = api(c, "GET", f"/reports/weekly?week_start=2026-10-12&unit_id={uid}", "gm1")
    assert week["totals"]["person_hours"] == 28 + stages
    assert week["totals"]["labor_cost"] == float(actual_labor)
    assert week["totals"]["stoppage_hours"] == 2
    assert week["units"][0]["end_percent"] == 100
    assert all(op["operation"].strip() and op["operation"] != "(بدون عملية)"
               for op in week["units"][0]["operations"])
    dash = api(c, "GET", "/dashboards/costs", "gm1")
    assert next(u for u in dash["units"] if u["unit_id"] == uid)["actual_total"] == float(expected_total)

    invoice = api(c, "POST", "/vendor-invoices/extract", "acc1", json={
        "purchase_order_id": po["id"], "supplier": po["supplier"],
        "invoice_no": f"QA-{method}-001", "invoice_date": "2026-10-15",
        "lines": [{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                   "quantity": 120, "uom": "Sheet", "unit_price": 92}],
        "tax_amount": 1656, "charges": 0, "total": 12696,
    })
    matched = api(c, "POST", f"/vendor-invoices/{invoice['id']}/match", "acc1")
    assert matched["match_result"]["type"] == "MATCHED"
    # Matching full PO invoice neither loads all 120 sheets onto this unit nor
    # adds recoverable tax/shipping automatically a second time.
    assert api(c, "GET", summary_path, "aud1")["actual"]["total"] == float(expected_total)
    traced_po = api(c, "GET", f"/purchase-orders/{po['id']}", "aud1")
    assert traced_po["material_request_id"] == mr["id"]
    assert approval["snapshot_hash"]
    assert grn["inspection_result"] == "ACCEPTED"
    for kind, entity, event in [
        ("material_issue", first_issue.json()["id"], "materials.issued"),
        ("goods_receipt", grn["id"], "grn.created"),
        ("bom", 1, "bom.approved"),
    ]:
        assert event in [e["event"] for e in api(c, "GET", f"/audit/{kind}/{entity}", "aud1")]
    # Complete unit cannot silently accumulate more production cost.
    api(c, "POST", "/progress-events", expected=400, json={
        "unit_id": uid, "work_order_id": first["id"],
        "percent_complete": 100, "hours": 1,
    })
    assert issue(c, uid, po["id"], 1).status_code == 400
    api(c, "POST", f"/production-units/{uid}/manufacturing-orders", expected=400)
    assert api(c, "GET", summary_path, "gm1")["actual"]["total"] == float(expected_total)


def test_duplicate_manufacturing_order_is_rejected(client):
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    api(client, "POST", f"/production-units/{uid}/manufacturing-orders", expected=409)
    assert len(_mos_wos(client, uid)) == 11


def test_completed_stage_rejects_more_labor_before_unit_completion(client):
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    wo = _mos_wos(client, uid)[0]
    body = {"unit_id": uid, "work_order_id": wo["id"], "percent_complete": 100, "hours": 1}
    api(client, "POST", "/progress-events", json=body)
    api(client, "POST", "/progress-events", expected=400, json=body)
    assert _mos_wos(client, uid)[0]["actual_hours"] == 1


@pytest.mark.parametrize("field,value", [
    ("quantity", -1), ("quantity", 0), ("estimated_unit_price", -1),
    ("quantity", "NaN"), ("estimated_unit_price", "Infinity"),
])
def test_invalid_bom_numbers(client, field, value):
    uid = _make_unit(client)["id"]
    body = {"description": "مادة", "quantity": 1, "estimated_unit_price": 1, field: value}
    response = client.post(f"/api/v1/production-units/{uid}/bom",
                           headers={"X-User": "eng1"}, json=[body])
    assert response.status_code in (400, 422), response.text


def test_zero_center_rate_does_not_use_nonzero_default(client, monkeypatch):
    from app.platform.config import get_settings
    monkeypatch.setattr(get_settings(), "default_labor_rate", 99)
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    wo = _mos_wos(client, uid)[0]
    wc = center(client, hourly_rate="0").json()
    api(client, "PATCH", f"/work-orders/{wo['id']}/plan",
        json={"work_center_id": wc["id"], "estimated_hours": 10})
    event = api(client, "POST", "/progress-events",
                json={"unit_id": uid, "work_order_id": wo["id"], "hours": 2, "percent_complete": 10})
    assert event["labor_cost"] == 0
    summary = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    assert summary["estimated"]["labor"] == 0
    assert summary["actual"]["labor"] == 0 and summary["flags"]["labor_rate_missing"]


def test_numeric_string_stoppage_is_normalized(client):
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    wo = _mos_wos(client, uid)[0]
    api(client, "POST", "/progress-events", json={
        "unit_id": uid, "work_order_id": wo["id"], "percent_complete": 10,
        "hours": 1, "stoppages": [{"hours": "2", "reason": "توقف"}],
    })
    dashboard = api(client, "GET", "/dashboards/factory", "gm1")
    assert dashboard["units"][0]["stoppage_hours"] == 2


@pytest.mark.parametrize("actor", ["proc1", "acc1", "fin1", "aud1"])
def test_manufacturing_write_role_guards(client, actor):
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    wo = _mos_wos(client, uid)[0]
    api(client, "POST", "/production-units", actor, 403, json={"house_no": "DENIED"})
    api(client, "POST", f"/production-units/{uid}/manufacturing-orders", actor, 403)
    api(client, "POST", f"/work-orders/{wo['id']}/start", actor, 403)
    api(client, "POST", "/progress-events", actor, 403,
        json={"unit_id": uid, "work_order_id": wo["id"], "hours": 1})
    api(client, "POST", f"/material-issues?unit_id={uid}", actor, 403,
        json={"purchase_order_id": 1, "quantity": 1})


def test_concurrent_manufacturing_order_creation(client):
    uid = _make_unit(client)["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _: client.post(f"/api/v1/production-units/{uid}/manufacturing-orders",
                                  headers={"X-User": "eng1"}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(_mos_wos(client, uid)) == 11


def test_concurrent_issues_cannot_overdraw_stock(client, monkeypatch):
    from app.modules.manufacturing import service
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    other = _make_unit(client)["id"]
    _create_mo(client, other)
    _, _, po, _ = purchase_and_receive(client, uid)
    original = service._issued_qty_by_item

    def slow_read(*args):
        values = original(*args)
        # Widen the real check-to-write race without a barrier/deadlock.
        time.sleep(.1)
        return values

    monkeypatch.setattr(service, "_issued_qty_by_item", slow_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda unit: issue(client, unit, po["id"], 80),
                                  [uid, other]))
    assert sorted(r.status_code for r in responses) == [200, 400]
    summaries = [api(client, "GET", f"/production-units/{unit}/cost-summary", "gm1")
                 for unit in (uid, other)]
    assert sum(s["actual"]["materials"] for s in summaries) == 7360


def test_invalid_material_issue_values_and_cross_unit_work_order(client):
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    other = _make_unit(client)["id"]
    _create_mo(client, other)
    foreign_wo = _mos_wos(client, other)[0]["id"]
    api(client, "POST", "/progress-events", expected=400, json={
        "unit_id": uid, "work_order_id": foreign_wo, "hours": 1,
    })
    _, _, po, _ = purchase_and_receive(client, uid)
    for quantity in (0, -1, "NaN", "Infinity"):
        response = issue(client, uid, po["id"], quantity)
        assert response.status_code in (400, 422)
    api(client, "POST", f"/material-issues?unit_id={uid}", "wh1", 400, json={
        "purchase_order_id": po["id"], "item_code": "GB-125",
        "quantity": 1, "uom": "",
    })
    summary = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    assert summary["actual"]["total"] == 0
    assert api(client, "GET", f"/production-units/{other}/progress")["overall_percent"] == 0


@pytest.mark.parametrize("value", [None, 0, 10])
def test_overhead_policy_is_explicit(client, monkeypatch, value):
    from app.platform.config import get_settings
    monkeypatch.setattr(get_settings(), "overhead_pct", value)
    monkeypatch.setattr(get_settings(), "default_labor_rate", 35)
    uid = _make_unit(client)["id"]
    _create_mo(client, uid)
    wo = _mos_wos(client, uid)[0]
    api(client, "POST", "/progress-events", json={
        "unit_id": uid, "work_order_id": wo["id"],
        "hours": 28, "percent_complete": 50,
    })
    summary = api(client, "GET", f"/production-units/{uid}/cost-summary", "gm1")
    expected_overhead = 98 if value == 10 else 0
    assert summary["actual"]["overhead_pct"] == value
    assert summary["actual"]["overhead"] == expected_overhead
    assert summary["actual"]["total"] == 980 + expected_overhead
    assert summary["flags"]["no_approved_bom"]
