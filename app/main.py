"""FDE platform — modular monolith entrypoint.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.platform.config import get_settings
from app.platform.database import Base, SessionLocal, engine
from app.platform.security import Role, User
from app.shared.errors import DomainError

logging.basicConfig(level=logging.INFO)

DEV_USERS = [
    ("eng1", "مهندس الموقع", Role.ENGINEER, "+966500000001"),
    ("proc1", "مسؤول المشتريات", Role.PROCUREMENT, "+966500000002"),
    ("gm1", "المدير العام", Role.GM, "+966500000003"),
    ("wh1", "أمين المستودع", Role.WAREHOUSE, "+966500000004"),
    ("acc1", "المحاسب", Role.ACCOUNTANT, "+966500000005"),
    ("fin1", "المدير المالي", Role.FINANCE, "+966500000006"),
    ("aud1", "المدقق", Role.AUDITOR, "+966500000007"),
]


def seed_dev_users() -> None:
    settings = get_settings()
    if not settings.dev_mode:
        return
    from app.platform.database import SessionLocal

    with SessionLocal() as db:
        for username, full_name, role, phone in DEV_USERS:
            if db.query(User).filter(User.username == username).first() is None:
                db.add(User(username=username, full_name=full_name,
                            role=role, phone=phone))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.platform import database

    database.Base.metadata.create_all(bind=database.engine)
    seed_dev_users()
    yield


app = FastAPI(
    title="FDE Platform",
    description="منصة تشغيل وموافقات لمصنع البيوت الجاهزة — Modular Monolith",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.detail, "error_type": type(exc).__name__},
    )


# --- routers ---------------------------------------------------------------
from app.modules.material_requests.router import router as mr_router  # noqa: E402
from app.modules.sourcing.router import router as sourcing_router  # noqa: E402
from app.modules.approvals.router import router as approvals_router  # noqa: E402
from app.modules.purchasing.router import router as purchasing_router  # noqa: E402
from app.modules.receiving.router import router as receiving_router  # noqa: E402
from app.modules.accounts_payable.router import router as ap_router  # noqa: E402
from app.modules.intake.router import router as intake_router  # noqa: E402
from app.modules.whatsapp.router import router as whatsapp_router  # noqa: E402
from app.modules.integrations.router import router as erp_router  # noqa: E402
from app.modules.audit.router import router as audit_router  # noqa: E402
from app.modules.manufacturing.router import router as mfg_router  # noqa: E402
from app.modules.manufacturing.landed_costs import router as landed_router  # noqa: E402
from app.modules.manufacturing.settlements import router as settlement_router  # noqa: E402
from app.modules.manufacturing.expenses import router as expenses_router  # noqa: E402
from app.modules.manufacturing.financial_close import router as close_router  # noqa: E402
from app.modules.manufacturing.work_centers import router as centers_router  # noqa: E402
from app.modules.manufacturing.reports import router as reports_router  # noqa: E402
from app.platform.identity import router as identity_router  # noqa: E402

app.include_router(identity_router)
app.include_router(audit_router)
app.include_router(mfg_router)
app.include_router(landed_router)
app.include_router(settlement_router)
app.include_router(expenses_router)
app.include_router(close_router)
app.include_router(centers_router)
app.include_router(reports_router)
app.include_router(mr_router)
app.include_router(sourcing_router)
app.include_router(approvals_router)
app.include_router(purchasing_router)
app.include_router(receiving_router)
app.include_router(ap_router)
app.include_router(intake_router)
app.include_router(whatsapp_router)
app.include_router(erp_router)


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "app": get_settings().app_name}


@app.get("/")
def index():
    from fastapi.responses import FileResponse

    return FileResponse("app/static/index.html")


@app.get("/operations")
def operations():
    from fastapi.responses import FileResponse
    return FileResponse("app/static/operations.html")


app.mount("/static", StaticFiles(directory="app/static"), name="static")
