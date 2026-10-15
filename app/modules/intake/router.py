from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.intake import service
from app.platform.database import get_db
from app.platform.security import current_user

router = APIRouter(prefix="/api/v1/intake", tags=["intake"])


class MessageIn(BaseModel):
    channel: str = "api"
    sender: str
    text: str = ""
    media_type: str | None = None


@router.post("/messages")
def ingest(body: MessageIn, db: Session = Depends(get_db),
           user=Depends(current_user)):
    def _create_draft(db_, msg):
        from app.modules.material_requests import service as mr_service
        from app.platform.config import get_settings
        from app.platform.database import SessionLocal

        # The draft is created on behalf of the sending user (matched by
        # username in dev mode; by phone number in production).
        with SessionLocal() as s:
            from app.platform.security import User
            sender_user = s.query(User).filter(User.username == body.sender).first()
        if sender_user is None:
            sender_user = user  # fall back to the authenticated caller
        return mr_service.create_request(
            db_, requester_id=sender_user.id,
            payload={"item_description": msg.body_text or ""},  # conservative
            raw_message=msg.body_text,
            channel="api",
        )

    msg, mr = service.ingest_message(
        db, channel=body.channel, sender=body.sender, text=body.text,
        media_type=body.media_type, create_draft_fn=_create_draft,
    )
    return {
        "message_id": msg.id,
        "classification": msg.ai_classification,
        "confidence": msg.ai_confidence,
        "draft_request_no": mr.request_no if mr else None,
    }
