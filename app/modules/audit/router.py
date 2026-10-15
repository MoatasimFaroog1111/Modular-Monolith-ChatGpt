from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.modules.audit import service
from app.platform.database import get_db
from app.platform.security import current_user

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("/{entity_type}/{entity_id}")
def entity_timeline(entity_type: str, entity_id: str,
                     db: Session = Depends(get_db), user=Depends(current_user)):
    events = service.timeline(db, entity_type, entity_id)
    return [
        {
            "id": e.id, "actor": e.actor, "event": e.event,
            "before": e.before, "after": e.after,
            "channel": e.channel, "result": e.result,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
