"""WhatsApp Cloud API webhook.

- GET: Meta hub-challenge verification.
- POST: message events. Signature (X-Hub-Signature-256) is verified when a
  webhook secret is configured; without a secret the endpoint refuses to
  process in production mode. Events are idempotent by message id — a
  redelivered webhook never creates a duplicate record.
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.platform.config import get_settings
from app.platform.database import get_db
from app.shared.idempotency import hash_payload

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])


@router.get("")
def verify(hub_mode: str = "", hub_challenge: str = "", hub_verify_token: str = ""):
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return int(hub_challenge)
    return {"ok": False}


@router.post("")
async def receive(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    body = await request.body()

    if settings.whatsapp_webhook_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            settings.whatsapp_webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            audit.record(
                db, actor="whatsapp", event="webhook.rejected",
                entity_type="whatsapp_event", entity_id="0",
                result="rejected", after={"reason": "bad signature"},
            )
            db.commit()
            return {"ok": False, "error": "invalid signature"}
    elif not settings.dev_mode:
        # unsigned webhooks are only tolerated in dev mode
        audit.record(
            db, actor="whatsapp", event="webhook.rejected",
            entity_type="whatsapp_event", entity_id="0",
            result="rejected", after={"reason": "no webhook secret configured"},
        )
        db.commit()
        return {"ok": False, "error": "webhook secret not configured"}

    import json

    try:
        payload = json.loads(body)
    except ValueError:
        return {"ok": False, "error": "invalid json"}

    # Idempotency: skip messages already ingested (same message id).
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                msg_id = message.get("id")
                if not msg_id:
                    continue
                # idempotency by message id via the audit trail
                already = (
                    db.query(audit_event_cls())
                    .filter(
                        audit_event_cls().entity_type == "whatsapp_event",
                        audit_event_cls().entity_id == msg_id,
                    )
                    .first()
                )
                if already:
                    continue
                sender = value.get("contacts", [{}])[0].get("wa_id", "unknown")
                text = (message.get("text") or {}).get("body", "")
                media = message.get("type") if message.get("type") != "text" else None

                from app.modules.intake import service as intake_service
                from app.modules.material_requests import service as mr_service
                from app.platform.security import User

                sender_user = (
                    db.query(User).filter(User.phone == sender).first()
                    if sender != "unknown" else None
                )
                if sender_user is None:
                    audit.record(
                        db, actor=sender, event="whatsapp.unlinked_sender",
                        entity_type="whatsapp_event", entity_id=msg_id,
                        result="rejected",
                        after={"note": "رقم غير مرتبط بمستخدم — الرسالة محفوظة للمراجعة"},
                    )
                    continue

                def _create_draft(db_, msg_obj, su=sender_user):
                    return mr_service.create_request(
                        db_, requester_id=su.id,
                        payload={"item_description": text},
                        raw_message=text, channel="whatsapp",
                    )

                intake_service.ingest_message(
                    db, channel="whatsapp", sender=sender, text=text,
                    media_type=media, create_draft_fn=_create_draft,
                )
                audit.record(
                    db, actor=sender, event="whatsapp.processed",
                    entity_type="whatsapp_event", entity_id=msg_id,
                )
    db.commit()
    return {"ok": True}


def audit_event_cls():
    from app.modules.audit.models import AuditEvent

    return AuditEvent
