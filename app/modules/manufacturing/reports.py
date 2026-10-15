"""Read-only Monday-Sunday reports, scoped by event date (Asia/Riyadh)."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.modules.manufacturing.models import ProgressEvent
from app.modules.projects.models import ManufacturingOrder, ProductionUnit, WorkOrder
from app.platform.database import get_db
from app.platform.security import Role, require_roles
from app.shared.errors import DomainError, NotFound

RIYADH = ZoneInfo("Asia/Riyadh")


def event_date(value: str | None) -> str:
    """Strict canonical date; no ambiguous local date formats."""
    if not value:
        return datetime.now(RIYADH).date().isoformat()
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise DomainError("تاريخ التقرير يجب أن يكون YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise DomainError("تاريخ التقرير يجب أن يكون YYYY-MM-DD")
    return parsed.isoformat()


def weekly_report(db: Session, week_start: date, unit_id: int | None = None,
                  project_id: int | None = None) -> dict:
    if week_start.weekday() != 0:
        raise DomainError("بداية التقرير الأسبوعي يجب أن تكون يوم الاثنين")
    week_end = week_start + timedelta(days=6)
    if unit_id is not None and db.get(ProductionUnit, unit_id) is None:
        raise NotFound("الوحدة غير موجودة")
    query = db.query(ProductionUnit)
    if unit_id is not None:
        query = query.filter(ProductionUnit.id == unit_id)
    if project_id is not None:
        query = query.filter(ProductionUnit.project_id == project_id)
    units = query.order_by(ProductionUnit.id).all()
    rows, invalid_events = [], 0
    for unit in units:
        work_orders = (
            db.query(WorkOrder).join(ManufacturingOrder, WorkOrder.mo_id == ManufacturingOrder.id)
            .filter(ManufacturingOrder.unit_id == unit.id)
            .order_by(WorkOrder.id).all()
        )
        work_ids = {wo.id for wo in work_orders}
        before, after = {wid: 0.0 for wid in work_ids}, {wid: 0.0 for wid in work_ids}
        events = db.query(ProgressEvent).filter_by(unit_id=unit.id).order_by(ProgressEvent.id).all()
        hours = labor = downtime = zero_rate_hours = 0.0
        count = 0
        shortages, stops, operations = [], [], {}
        for ev in events:
            try:
                day = date.fromisoformat(ev.event_date)
                if day.isoformat() != ev.event_date:
                    raise ValueError("non-canonical")
            except (ValueError, TypeError):
                invalid_events += 1
                continue
            if day <= week_end and ev.work_order_id in work_ids:
                after[ev.work_order_id] = max(after[ev.work_order_id], ev.percent_complete)
                if day < week_start:
                    before[ev.work_order_id] = max(before[ev.work_order_id], ev.percent_complete)
            if not week_start <= day <= week_end:
                continue
            count += 1
            hours += ev.hours
            labor += ev.labor_cost
            if ev.labor_rate_used == 0:
                zero_rate_hours += ev.hours
            operation = ev.operation or "(بدون عملية)"
            aggregate = operations.setdefault(operation, {"person_hours": 0.0, "labor_cost": 0.0})
            aggregate["person_hours"] += ev.hours
            aggregate["labor_cost"] += ev.labor_cost
            for stop in ev.stoppages or []:
                stop_hours = float(stop.get("hours", 0))
                downtime += stop_hours
                stops.append({"event_id": ev.id, "date": ev.event_date,
                              "hours": stop_hours, "reason": stop.get("reason", "")})
            if ev.material_shortage:
                shortages.append({
                    "event_id": ev.id, "date": ev.event_date,
                    "description": ev.shortage_description,
                    "linked_material_request_id": ev.linked_material_request_id,
                })
        start_percent = round(sum(before.values()) / len(before), 2) if before else 0.0
        end_percent = round(sum(after.values()) / len(after), 2) if after else 0.0
        rows.append({
            "unit_id": unit.id, "house_no": unit.house_no, "build_method": unit.build_method,
            "project_id": unit.project_id, "events_count": count,
            "start_percent": start_percent, "end_percent": end_percent,
            "progress_delta_points": round(end_percent - start_percent, 2),
            "person_hours": round(hours, 2), "labor_cost": round(labor, 2),
            "zero_or_unconfigured_rate_hours": round(zero_rate_hours, 2),
            "stoppage_hours": round(downtime, 2), "shortages": shortages, "stoppages": stops,
            "operations": [{"operation": op, **{k: round(v, 2) for k, v in vals.items()}}
                           for op, vals in operations.items()],
            "work_order_count": len(work_orders),
        })
    return {
        "week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
        "timezone": "Asia/Riyadh", "currency": "SAR",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "progress_basis": "unweighted_mean_current_work_orders_as_of_event_date",
        "warnings": {"legacy_invalid_event_dates_skipped": invalid_events},
        "totals": {
            "units": len(rows), "events": sum(r["events_count"] for r in rows),
            "person_hours": round(sum(r["person_hours"] for r in rows), 2),
            "labor_cost": round(sum(r["labor_cost"] for r in rows), 2),
            "stoppage_hours": round(sum(r["stoppage_hours"] for r in rows), 2),
            "shortage_reports": sum(len(r["shortages"]) for r in rows),
            "zero_or_unconfigured_rate_hours": round(sum(r["zero_or_unconfigured_rate_hours"] for r in rows), 2),
        },
        "units": rows,
    }


def csv_safe(value):
    text = str(value)
    # Prevent spreadsheet formula execution, including whitespace-prefixed inputs.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def weekly_csv(report: dict) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["week_start", "week_end", "unit_id", "house_no", "build_method",
                     "start_percent", "end_percent", "progress_delta_points",
                     "person_hours", "labor_cost_SAR", "stoppage_hours", "shortage_reports"])
    for row in report["units"]:
        values = [report["week_start"], report["week_end"], row["unit_id"], row["house_no"],
                  row["build_method"], row["start_percent"], row["end_percent"],
                  row["progress_delta_points"], row["person_hours"], row["labor_cost"],
                  row["stoppage_hours"], len(row["shortages"])]
        writer.writerow([csv_safe(v) for v in values])
    return "\ufeff" + out.getvalue()


router = APIRouter(prefix="/api/v1/reports", tags=["weekly-reports"])
read_access = require_roles(Role.ENGINEER, Role.GM, Role.FINANCE, Role.ACCOUNTANT, Role.AUDITOR)


@router.get("/weekly")
def get_weekly(week_start: date, unit_id: int | None = Query(default=None, gt=0),
               project_id: int | None = Query(default=None, gt=0),
               db: Session = Depends(get_db), actor=Depends(read_access)):
    return weekly_report(db, week_start, unit_id, project_id)


@router.get("/weekly.csv")
def get_weekly_csv(week_start: date, unit_id: int | None = Query(default=None, gt=0),
                   project_id: int | None = Query(default=None, gt=0),
                   db: Session = Depends(get_db), actor=Depends(read_access)):
    report = weekly_report(db, week_start, unit_id, project_id)
    return Response(weekly_csv(report), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="fde-weekly-{week_start}.csv"'})
