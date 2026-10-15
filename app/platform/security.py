"""Identity, roles and RBAC.

Dev authentication: X-User header carrying the username.  This is deliberately
simple for the prototype; production deployments must replace `current_user`
with an OIDC provider (Keycloak / Auth0 / Entra ID) and enable MFA for
approvers and accountants.  Role checks are kept server-side in services.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.config import get_settings
from app.platform.database import Base


class Role:
    ENGINEER = "engineer"
    PROCUREMENT = "procurement"
    GM = "gm"                    # general manager
    WAREHOUSE = "warehouse"
    ACCOUNTANT = "accountant"
    FINANCE = "finance"
    ADMIN = "admin"
    AUDITOR = "auditor"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(32), index=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


def current_user(x_user: str | None = Header(default=None, alias="X-User")) -> User:
    """Dev-mode header auth. Disabled outside dev mode — production must use
    an OIDC provider (Keycloak / Auth0 / Entra ID) with MFA for approvers."""
    settings = get_settings()
    if not settings.dev_mode:
        raise HTTPException(
            status_code=401,
            detail="مصادقة X-User معطلة خارج وضع التطوير — يتطلب مزود OIDC",
        )
    if not x_user:
        raise HTTPException(status_code=401, detail="X-User header مطلوب")
    from app.platform.database import SessionLocal

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == x_user).first()
        if user is None:
            raise HTTPException(status_code=401, detail=f"مستخدم غير معروف: {x_user}")
        return user


def require_roles(*roles: str):
    """Dependency factory enforcing that the caller holds one of `roles`."""

    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"الدور '{user.role}' غير مخول لهذا الإجراء (المطلوب: {', '.join(roles)})",
            )
        return user

    return checker


def require_any_role() -> User:
    return Depends(current_user)
