from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.modules.manufacturing import costing, service
from app.platform.database import get_db
from app.platform.security import Role, current_user, require_roles

router = APIRouter(prefix="/api/v1", tags=["manufacturing"])


class UnitIn(BaseModel):
    house_no: str
    project_id: int | None = None
    build_method: str = "CONTAINER"     # CONTAINER | FRAMECAD
    model: str = ""


class BoMLineIn(BaseModel):
    item_code: str | None = None
    description: str
    quantity: float = Field(gt=0, allow_inf_nan=False)
    uom: str = ""
    estimated_unit_price: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class ProgressIn(BaseModel):
    unit_id: int
    work_order_id: int | None = None
    operation: str = ""
    percent_complete: float = Field(default=0.0, ge=0, le=100, allow_inf_nan=False)
    workers: int = Field(default=0, ge=0)
    hours: float = Field(default=0.0, ge=0, allow_inf_nan=False)  # ساعات أشخاص:
                           # 4 فنيين × 7 ساعات = 28، وليس 7
    stoppages: list[dict] = []         # [{hours, reason}]
    material_shortage: bool = False
    shortage_description: str | None = None
    event_date: str = ""


class IssueLineIn(BaseModel):
    item_code: str | None = None
    description: str = ""
    quantity: float
    uom: str = ""
    purchase_order_id: int


@router.post("/production-units")
def create_unit(body: UnitIn, db: Session = Depends(get_db),
                user=Depends(require_roles(Role.ENGINEER, Role.ADMIN))):
    unit = service.create_unit(db, actor=user, payload=body.model_dump())
    return {"id": unit.id, "house_no": unit.house_no,
            "build_method": unit.build_method, "status": unit.status}


@router.post("/production-units/{unit_id}/bom")
def create_bom(unit_id: int, body: list[BoMLineIn], db: Session = Depends(get_db),
               user=Depends(require_roles(Role.ENGINEER, Role.PROCUREMENT, Role.ADMIN))):
    bom = service.create_bom(
        db, actor=user, unit_id=unit_id,
        lines=[l.model_dump() for l in body],
    )
    return {"id": bom.id, "version": bom.version, "status": bom.status}


@router.post("/boms/{bom_id}/approve")
def approve_bom(bom_id: int, db: Session = Depends(get_db),
                user=Depends(require_roles(Role.GM, Role.FINANCE, Role.ADMIN))):
    bom = service.approve_bom(db, actor=user, bom_id=bom_id)
    return {"id": bom.id, "version": bom.version, "status": bom.status}


@router.post("/production-units/{unit_id}/manufacturing-orders")
def create_mo(unit_id: int, db: Session = Depends(get_db),
              user=Depends(require_roles(Role.ENGINEER, Role.ADMIN))):
    mo = service.create_mo(db, actor=user, unit_id=unit_id)
    return {"id": mo.id, "status": mo.status}


@router.post("/work-orders/{wo_id}/start")
def start_wo(wo_id: int, db: Session = Depends(get_db),
             user=Depends(require_roles(Role.ENGINEER, Role.ADMIN))):
    wo = service.start_wo(db, actor=user, wo_id=wo_id)
    return {"id": wo.id, "operation": wo.operation, "status": wo.status}


@router.post("/progress-events")
def report_progress(body: ProgressIn, db: Session = Depends(get_db),
                    user=Depends(require_roles(Role.ENGINEER, Role.WAREHOUSE))):
    ev = service.report_progress(db, actor=user, payload=body.model_dump())
    return {
        "id": ev.id, "unit_id": ev.unit_id, "operation": ev.operation,
        "percent_complete": ev.percent_complete, "labor_cost": ev.labor_cost,
        "linked_material_request_id": ev.linked_material_request_id,
    }


@router.post("/material-issues")
def issue_materials(body: IssueLineIn, unit_id: int, db: Session = Depends(get_db),
                   user=Depends(require_roles(Role.WAREHOUSE))):
    """Single-line issue per call — kept simple for the prototype."""
    issue = service.issue_materials(
        db, actor=user, unit_id=unit_id, lines=[body.model_dump()],
    )
    return {"id": issue.id, "issue_no": issue.issue_no,
            "materials_cost": issue.materials_cost()}


@router.get("/production-units/{unit_id}/progress")
def unit_progress(unit_id: int, db: Session = Depends(get_db),
                  user=Depends(current_user)):
    return service.unit_progress(db, unit_id)


@router.get("/production-units/{unit_id}/cost-summary")
def cost_summary(unit_id: int, db: Session = Depends(get_db),
                 user=Depends(require_roles(Role.GM, Role.FINANCE, Role.ACCOUNTANT,
                                            Role.AUDITOR, Role.ENGINEER))):
    return costing.unit_cost_summary(db, unit_id)


@router.get("/dashboards/factory")
def factory_dashboard(db: Session = Depends(get_db),
                      user=Depends(require_roles(Role.ENGINEER, Role.GM, Role.ADMIN,
                                                 Role.AUDITOR, Role.WAREHOUSE))):
    return costing.factory_dashboard(db)


@router.get("/dashboards/costs")
def costs_dashboard(db: Session = Depends(get_db),
                    user=Depends(require_roles(Role.GM, Role.FINANCE, Role.AUDITOR))):
    return costing.cost_deviation_dashboard(db)
