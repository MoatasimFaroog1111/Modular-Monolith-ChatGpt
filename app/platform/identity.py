"""Authenticated caller metadata for UI permissions; never a replacement for RBAC."""
from fastapi import APIRouter, Depends

from app.platform.security import User, current_user

router = APIRouter(prefix="/api/v1", tags=["identity"])


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "full_name": user.full_name,
            "role": user.role}
