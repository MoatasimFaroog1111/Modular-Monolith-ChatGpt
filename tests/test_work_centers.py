from tests.test_manufacturing import _create_mo, _make_unit, _mos_wos


def center(client, **overrides):
    body = {"name": "تجميع الفريمات", "hourly_rate": "35.00"}
    body.update(overrides)
    return client.post("/api/v1/work-centers", headers={"X-User": "gm1"}, json=body)


def test_work_center_crud_audit_and_version_guard(client):
    created = center(client)
    assert created.status_code == 200
    row = created.json()
    assert row["version"] == 1 and row["is_active"]
    assert center(client).status_code == 409
    assert center(client, name=" ", hourly_rate="-1").status_code == 422
    update = {"name": "تجميع معدني", "hourly_rate": 45, "is_active": True, "version": 1}
    url = f"/api/v1/work-centers/{row['id']}"
    assert client.patch(url, headers={"X-User": "eng1"}, json=update).status_code == 403
    changed = client.patch(url, headers={"X-User": "gm1"}, json=update)
    assert changed.status_code == 200 and changed.json()["version"] == 2
    assert client.patch(url, headers={"X-User": "gm1"}, json=update).status_code == 409
    events = client.get(f"/api/v1/audit/work_center/{row['id']}", headers={"X-User": "aud1"}).json()
    assert events[1]["before"]["hourly_rate"] == 35
    assert events[1]["after"]["hourly_rate"] == 45
    listing = client.get("/api/v1/work-centers", headers={"X-User": "eng1"}).json()
    assert listing[0]["name"] == "تجميع معدني"


def test_planning_and_historical_labor_rate(client):
    c = center(client).json()
    u = _make_unit(client)
    _create_mo(client, u["id"])
    wo = _mos_wos(client, u["id"])[0]
    plan_url = f"/api/v1/work-orders/{wo['id']}/plan"
    bad = client.patch(plan_url, headers={"X-User": "eng1"},
                       json={"work_center_id": 99999, "estimated_hours": 20})
    assert bad.status_code == 400
    plan = {"work_center_id": c["id"], "estimated_hours": 28}
    assert client.patch(plan_url, headers={"X-User": "eng1"}, json=plan).status_code == 200
    progress = {"unit_id": u["id"], "work_order_id": wo["id"], "hours": 28,
                "workers": 4, "percent_complete": 30}
    assert client.post("/api/v1/progress-events", json=progress,
                       headers={"X-User": "eng1"}).json()["labor_cost"] == 980
    assert client.patch(plan_url, headers={"X-User": "eng1"}, json=plan).status_code == 409
    center_url = f"/api/v1/work-centers/{c['id']}"
    assert client.patch(center_url, headers={"X-User": "gm1"},
                        json={"name": c["name"], "hourly_rate": 40, "version": 1}).status_code == 200
    summary = client.get(f"/api/v1/production-units/{u['id']}/cost-summary",
                         headers={"X-User": "gm1"}).json()
    assert summary["actual"]["labor"] == 980
    # Cannot deactivate a center while work is in progress.
    assert client.patch(center_url, headers={"X-User": "gm1"},
                        json={"name": c["name"], "hourly_rate": 40, "version": 2,
                              "is_active": False}).status_code == 409
    assert client.post("/api/v1/progress-events", json={**progress, "hours": 1},
                       headers={"X-User": "eng1"}).json()["labor_cost"] == 40


def test_inactive_center_and_zero_rate(client):
    c = center(client, hourly_rate="0").json()
    url = f"/api/v1/work-centers/{c['id']}"
    assert client.patch(url, headers={"X-User": "gm1"},
                        json={"name": c["name"], "hourly_rate": 0, "version": 1,
                              "is_active": False}).status_code == 200
    u = _make_unit(client); _create_mo(client, u["id"])
    wo = _mos_wos(client, u["id"])[0]
    assert client.patch(f"/api/v1/work-orders/{wo['id']}/plan", headers={"X-User": "eng1"},
                        json={"work_center_id": c["id"], "estimated_hours": 1}).status_code == 400
    assert client.get("/api/v1/production-units", headers={"X-User": "eng1"}).status_code == 200
    assert client.get(f"/api/v1/production-units/{u['id']}/work-orders",
                      headers={"X-User": "eng1"}).json()[0]["id"] == wo["id"]
