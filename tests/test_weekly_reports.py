import csv
import io

from tests.test_manufacturing import _create_mo, _make_unit, _mos_wos


def test_weekly_boundaries_deltas_and_export(client):
    unit = _make_unit(client)
    _create_mo(client, unit["id"])
    wo = _mos_wos(client, unit["id"])[0]
    c = client.post("/api/v1/work-centers", headers={"X-User": "gm1"},
                    json={"name": "ورشة", "hourly_rate": 35}).json()
    client.patch(f"/api/v1/work-orders/{wo['id']}/plan", headers={"X-User": "eng1"},
                 json={"work_center_id": c["id"], "estimated_hours": 28})
    for day, percent, hours in [("2026-10-11", 10, 1), ("2026-10-12", 30, 4),
                                ("2026-10-18", 60, 6), ("2026-10-19", 90, 9)]:
        response = client.post("/api/v1/progress-events", headers={"X-User": "eng1"}, json={
            "unit_id": unit["id"], "work_order_id": wo["id"], "event_date": day,
            "percent_complete": percent, "hours": hours, "workers": 2,
            "stoppages": [{"hours": 2, "reason": "نقص مواد"}] if day == "2026-10-18" else [],
            "material_shortage": day == "2026-10-18", "shortage_description": "براغي",
        })
        assert response.status_code == 200, response.text
    report = client.get("/api/v1/reports/weekly?week_start=2026-10-12",
                        headers={"X-User": "gm1"}).json()
    assert report["week_end"] == "2026-10-18"
    assert report["totals"]["person_hours"] == 10
    assert report["totals"]["labor_cost"] == 350
    assert report["totals"]["stoppage_hours"] == 2
    row = report["units"][0]
    assert row["start_percent"] == round(10 / 11, 2)
    assert row["end_percent"] == round(60 / 11, 2)
    assert len(row["shortages"]) == 1
    assert row["shortages"][0]["linked_material_request_id"] is not None
    assert report["totals"]["events"] == 2
    csv_response = client.get("/api/v1/reports/weekly.csv?week_start=2026-10-12",
                              headers={"X-User": "aud1"})
    assert csv_response.status_code == 200
    assert "attachment;" in csv_response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert rows[1][3] == "H-07"
    assert rows[1][8] == "10.0"


def test_weekly_empty_filters_invalid_dates_and_permissions(client):
    empty = client.get("/api/v1/reports/weekly?week_start=2026-10-12",
                       headers={"X-User": "aud1"}).json()
    assert empty["units"] == [] and empty["totals"]["person_hours"] == 0
    assert client.get("/api/v1/reports/weekly?week_start=2026-10-13",
                      headers={"X-User": "gm1"}).status_code == 400
    assert client.get("/api/v1/reports/weekly?week_start=invalid",
                      headers={"X-User": "gm1"}).status_code == 422
    assert client.get("/api/v1/reports/weekly?week_start=2026-10-12",
                      headers={"X-User": "wh1"}).status_code == 403
    assert client.get("/api/v1/reports/weekly?week_start=2026-10-12&unit_id=999",
                      headers={"X-User": "gm1"}).status_code == 404
    u = _make_unit(client)
    project = client.get("/api/v1/reports/weekly?week_start=2026-10-12&project_id=999",
                         headers={"X-User": "gm1"}).json()
    assert project["units"] == []
    selected = client.get(f"/api/v1/reports/weekly?week_start=2026-10-12&unit_id={u['id']}",
                          headers={"X-User": "gm1"}).json()
    assert len(selected["units"]) == 1 and selected["units"][0]["person_hours"] == 0
    for day in ["2026-13-45", "20261012", "12/10/2026"]:
        response = client.post("/api/v1/progress-events", headers={"X-User": "eng1"},
                               json={"unit_id": u["id"], "event_date": day})
        assert response.status_code == 400
    response = client.post("/api/v1/progress-events", headers={"X-User": "eng1"},
                           json={"unit_id": u["id"], "stoppages": [{"hours": -1, "reason": "x"}]})
    assert response.status_code == 400


def test_weekly_csv_formula_safety(client):
    client.post("/api/v1/production-units", headers={"X-User": "eng1"},
                json={"house_no": "=1+1", "build_method": "CONTAINER"})
    response = client.get("/api/v1/reports/weekly.csv?week_start=2026-10-12",
                           headers={"X-User": "gm1"})
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[1][3] == "'=1+1"
