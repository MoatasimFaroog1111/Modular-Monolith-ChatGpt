"""Phase 3 tests: manufacturing orders, progress, material issues, actual cost."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_full_cycle import _create_mr, _run_to_pending_gm


def _make_unit(client: TestClient, method: str = "FRAMECAD") -> dict:
    res = client.post(
        "/api/v1/production-units",
        json={"house_no": "H-07", "build_method": method, "model": "GR-40"},
        headers={"X-User": "eng1"},
    )
    assert res.status_code == 200, res.text
    return res.json()


def _add_bom(client: TestClient, unit_id: int) -> dict:
    res = client.post(
        f"/api/v1/production-units/{unit_id}/bom",
        json=[
            {"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
             "quantity": 100, "uom": "Sheet", "estimated_unit_price": 92.0},
            {"item_code": "SC-8", "description": "براغي 8mm",
             "quantity": 20, "uom": "BOX", "estimated_unit_price": 45.0},
        ],
        headers={"X-User": "eng1"},
    )
    assert res.status_code == 200, res.text
    # the costing baseline must be an APPROVED version
    app = client.post(f"/api/v1/boms/{res.json()['id']}/approve",
                      headers={"X-User": "gm1"})
    assert app.status_code == 200, app.text
    return res.json()


def _create_mo(client: TestClient, unit_id: int) -> dict:
    res = client.post(f"/api/v1/production-units/{unit_id}/manufacturing-orders",
                      headers={"X-User": "eng1"})
    assert res.status_code == 200, res.text
    return res.json()


def _mos_wos(client: TestClient, unit_id: int) -> list[dict]:
    progress = client.get(f"/api/v1/production-units/{unit_id}/progress",
                          headers={"X-User": "eng1"}).json()
    return progress["work_orders"]


def test_mo_from_framecad_template_with_dependencies(client: TestClient):
    unit = _make_unit(client, "FRAMECAD")
    _add_bom(client, unit["id"])
    mo = _create_mo(client, unit["id"])

    wos = _mos_wos(client, unit["id"])
    assert len(wos) == 11          # FRAMECAD stages
    assert wos[0]["operation"] == "التصميم المعتمد"
    assert wos[0]["blocked_by_dependency"] is False
    # every later stage is blocked until its predecessor finishes
    assert all(w["blocked_by_dependency"] for w in wos[1:])
    assert mo["status"] == "CONFIRMED"


def test_mo_from_container_template(client: TestClient):
    unit = _make_unit(client, "CONTAINER")
    _create_mo(client, unit["id"])
    wos = _mos_wos(client, unit["id"])
    assert len(wos) == 10          # CONTAINER stages
    assert wos[0]["operation"] == "استلام الحاوية"


def test_work_order_dependency_guard(client: TestClient):
    unit = _make_unit(client)
    _create_mo(client, unit["id"])
    wos = _mos_wos(client, unit["id"])
    wo2 = wos[1]

    # starting stage 2 before stage 1 is done → refused
    res = client.post(f"/api/v1/work-orders/{wo2['id']}/start",
                      headers={"X-User": "eng1"})
    assert res.status_code == 400
    assert "تبعية" in res.json()["detail"] or "قبل انتهاء" in res.json()["detail"]

    # reporting progress on stage 1 to 100% completes it
    ev = client.post(
        "/api/v1/progress-events",
        json={"unit_id": unit["id"], "work_order_id": wos[0]["id"],
              "percent_complete": 100, "workers": 4, "hours": 7},
        headers={"X-User": "eng1"},
    )
    assert ev.status_code == 200, ev.text
    assert ev.json()["labor_cost"] == 0.0   # no rate configured → flagged, not invented

    # now stage 2 can start
    ok = client.post(f"/api/v1/work-orders/{wo2['id']}/start",
                     headers={"X-User": "eng1"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "IN_PROGRESS"


def test_progress_with_work_center_rate_and_stoppages(client: TestClient):
    from app.platform.config import get_settings
    from app.platform.database import SessionLocal
    from app.modules.manufacturing import models as mfg

    with SessionLocal() as db:
        db.add(mfg.WorkCenter(name="التجميع", hourly_rate=35.0))
        db.commit()

    unit = _make_unit(client)
    _create_mo(client, unit["id"])
    wos = _mos_wos(client, unit["id"])

    # attach the work center to stage 1
    with SessionLocal() as db:
        from app.modules.projects.models import WorkOrder
        wo = db.get(WorkOrder, wos[0]["id"])
        wo.work_center_id = 1
        db.commit()

    ev = client.post(
        "/api/v1/progress-events",
        json={
            "unit_id": unit["id"], "work_order_id": wos[0]["id"],
            "percent_complete": 60, "workers": 4, "hours": 28,
            "stoppages": [{"hours": 2, "reason": "نقص البراغي"}],
        },
        headers={"X-User": "eng1"},
    )
    assert ev.status_code == 200, ev.text
    # 28h × 35 SAR = 980 — actual labor, not an estimate
    assert ev.json()["labor_cost"] == 980.0

    dash = client.get("/api/v1/dashboards/factory",
                      headers={"X-User": "gm1"}).json()
    row = [u for u in dash["units"] if u["unit_id"] == unit["id"]][0]
    assert row["stoppage_hours"] == 2.0
    assert row["actual_hours"] == 28.0


def test_material_shortage_spawns_linked_request(client: TestClient):
    unit = _make_unit(client)
    _create_mo(client, unit["id"])
    wos = _mos_wos(client, unit["id"])

    ev = client.post(
        "/api/v1/progress-events",
        json={
            "unit_id": unit["id"], "work_order_id": wos[0]["id"],
            "operation": wos[0]["operation"],
            "percent_complete": 30, "workers": 5, "hours": 6,
            "material_shortage": True,
            "shortage_description": "براغي 8mm — الكمية غير كافية لإكمال الفريمات",
        },
        headers={"X-User": "eng1"},
    )
    assert ev.status_code == 200, ev.text
    linked = ev.json()["linked_material_request_id"]
    assert linked is not None

    mr = client.get(f"/api/v1/material-requests/{linked}",
                    headers={"X-User": "eng1"}).json()
    assert mr["production_unit_id"] == unit["id"]
    assert mr["priority"] == "STOPPING"

    progress = client.get(f"/api/v1/production-units/{unit['id']}/progress",
                          headers={"X-User": "eng1"}).json()
    assert progress["open_material_shortages"][0]["linked_mr"] == linked


def test_material_issue_costed_from_po_and_guarded(client: TestClient):
    """End-to-end: request → quotes → approval → PO → GRN → issue to unit →
    actual cost appears in the unit's cost summary against its BoM."""
    unit = _make_unit(client, "FRAMECAD")
    _add_bom(client, unit["id"])   # est: 100×92 + 20×45 = 10100
    _create_mo(client, unit["id"])

    # --- procurement cycle for GB-125 ------------------------------------
    mr = _create_mr(client)
    ctx = _run_to_pending_gm(client, mr["id"])
    chosen = ctx["quotes"][0]
    dec = client.post(
        f"/api/v1/approvals/{mr['id']}/decision",
        json={"quotation_id": chosen["quotation_id"], "decision": "APPROVED"},
        headers={"X-User": "gm1"},
    )
    assert dec.status_code == 200
    po = client.post(f"/api/v1/purchase-orders?mr_id={mr['id']}",
                     headers={"X-User": "proc1"}).json()

    grn = client.post(
        "/api/v1/receipts",
        json={"purchase_order_id": po["id"],
              "lines": [{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                         "qty_required": 120, "qty_supplied": 120,
                         "qty_accepted": 120, "qty_rejected": 0, "condition": "OK"}]},
        headers={"X-User": "wh1"},
    )
    assert grn.status_code == 200, grn.text

    # --- issue 100 sheets to the unit at the PO price (92) -----------------
    issue = client.post(
        f"/api/v1/material-issues?unit_id={unit['id']}",
        json={"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
              "quantity": 100, "uom": "Sheet", "purchase_order_id": po["id"]},
        headers={"X-User": "wh1"},
    )
    assert issue.status_code == 200, issue.text
    assert issue.json()["materials_cost"] == 9200.0

    # over-issue beyond accepted quantity is refused
    over = client.post(
        f"/api/v1/material-issues?unit_id={unit['id']}",
        json={"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
              "quantity": 30, "uom": "Sheet", "purchase_order_id": po["id"]},
        headers={"X-User": "wh1"},
    )
    assert over.status_code == 400
    assert "المتاح" in over.json()["detail"]

    # only warehouse can issue
    denied = client.post(
        f"/api/v1/material-issues?unit_id={unit['id']}",
        json={"item_code": "GB-125", "description": "x",
              "quantity": 1, "uom": "Sheet", "purchase_order_id": po["id"]},
        headers={"X-User": "eng1"},
    )
    assert denied.status_code == 403

    # --- cost summary: actual vs BoM ---------------------------------------
    summary = client.get(f"/api/v1/production-units/{unit['id']}/cost-summary",
                         headers={"X-User": "gm1"}).json()
    assert summary["estimated"]["materials"] == 10100.0
    assert summary["actual"]["materials"] == 9200.0
    assert summary["deviation"]["materials"] == -900.0

    dash = client.get("/api/v1/dashboards/costs",
                      headers={"X-User": "gm1"}).json()
    row = [u for u in dash["units"] if u["unit_id"] == unit["id"]][0]
    assert row["actual_total"] == 9200.0
    assert row["deviation_total"] == -900.0


def test_bom_draft_does_not_change_costing_baseline(client: TestClient):
    unit = _make_unit(client)
    _add_bom(client, unit["id"])            # approved: 100×92 + 20×45 = 10100
    _create_mo(client, unit["id"])

    # a later DRAFT with inflated prices must not move the baseline
    res = client.post(
        f"/api/v1/production-units/{unit['id']}/bom",
        json=[{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
               "quantity": 100, "uom": "Sheet", "estimated_unit_price": 500.0}],
        headers={"X-User": "eng1"},
    )
    assert res.status_code == 200

    summary = client.get(f"/api/v1/production-units/{unit['id']}/cost-summary",
                         headers={"X-User": "gm1"}).json()
    assert summary["estimated"]["materials"] == 10100.0
    assert summary["bom_version"] == 1
    assert summary["valuation"] == "provisional_po_price"


def test_issue_guards_negative_uom_and_qc_hold(client: TestClient):
    unit = _make_unit(client)
    _add_bom(client, unit["id"])
    _create_mo(client, unit["id"])

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

    # GRN with 50 accepted + 30 rejected → QC_HOLD (nothing available for issue)
    grn = client.post(
        "/api/v1/receipts",
        json={"purchase_order_id": po["id"],
              "lines": [{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                         "qty_required": 120, "qty_supplied": 80,
                         "qty_accepted": 50, "qty_rejected": 30, "condition": "DAMAGED"}]},
        headers={"X-User": "wh1"},
    )
    assert grn.json()["inspection_result"] == "QC_HOLD"

    # issue from quality-held stock is refused (nothing available)
    held = client.post(
        f"/api/v1/material-issues?unit_id={unit['id']}",
        json={"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
              "quantity": 10, "uom": "Sheet", "purchase_order_id": po["id"]},
        headers={"X-User": "wh1"},
    )
    assert held.status_code == 400

    # a clean GRN makes 120 available
    client.post(
        "/api/v1/receipts",
        json={"purchase_order_id": po["id"],
              "lines": [{"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
                         "qty_required": 120, "qty_supplied": 120,
                         "qty_accepted": 120, "qty_rejected": 0, "condition": "OK"}]},
        headers={"X-User": "wh1"},
    )

    # negative quantity refused
    neg = client.post(
        f"/api/v1/material-issues?unit_id={unit['id']}",
        json={"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
              "quantity": -5, "uom": "Sheet", "purchase_order_id": po["id"]},
        headers={"X-User": "wh1"},
    )
    assert neg.status_code == 400

    # unit mismatch refused (PO is per Sheet, issue claims M2)
    uom = client.post(
        f"/api/v1/material-issues?unit_id={unit['id']}",
        json={"item_code": "GB-125", "description": "Gypsum Board MR 12.5mm",
              "quantity": 5, "uom": "M2", "purchase_order_id": po["id"]},
        headers={"X-User": "wh1"},
    )
    assert uom.status_code == 400
    assert "معامل تحويل" in uom.json()["detail"]


def test_mo_and_unit_complete_when_all_work_orders_done(client: TestClient):
    unit = _make_unit(client, "CONTAINER")   # 10 stages
    _add_bom(client, unit["id"])
    _create_mo(client, unit["id"])
    wos = _mos_wos(client, unit["id"])
    assert len(wos) == 10

    for wo in wos:
        ev = client.post(
            "/api/v1/progress-events",
            json={"unit_id": unit["id"], "work_order_id": wo["id"],
                  "percent_complete": 100, "workers": 4, "hours": 8},
            headers={"X-User": "eng1"},
        )
        assert ev.status_code == 200, ev.text

    progress = client.get(f"/api/v1/production-units/{unit['id']}/progress",
                          headers={"X-User": "eng1"}).json()
    assert progress["overall_percent"] == 100.0
    assert all(w["status"] == "DONE" for w in progress["work_orders"])

    from app.platform.database import SessionLocal
    from app.modules.projects.models import ManufacturingOrder, ProductionUnit
    with SessionLocal() as db:
        mo = db.query(ManufacturingOrder).filter_by(unit_id=unit["id"]).first()
        assert mo.status == "DONE"
        assert db.get(ProductionUnit, unit["id"]).status == "DONE"
