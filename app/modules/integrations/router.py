from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.modules.accounts_payable import service as ap_service
from app.modules.integrations import hub
from app.platform.config import get_settings
from app.platform.database import get_db

router = APIRouter(prefix="/webhooks/erp", tags=["integrations"])


@router.post("/{connector_name}")
async def erp_webhook(connector_name: str, request: Request,
                      db: Session = Depends(get_db)):
    """ERP-side callbacks. Authenticated with a shared secret — posting
    confirmations are never accepted anonymously. Idempotent by
    (connector, internal id, event); a repeated delivery never creates a
    second financial record."""
    import json

    settings = get_settings()
    secret = request.headers.get("X-ERP-Webhook-Secret", "")
    if not settings.erp_webhook_secret or not hmac.compare_digest(
        secret, settings.erp_webhook_secret
    ):
        return {"ok": False, "error": "invalid erp webhook secret"}

    try:
        payload = json.loads(await request.body())
    except ValueError:
        return {"ok": False, "error": "invalid json"}

    event = payload.get("event")
    internal_entity = payload.get("internal_entity", "vendor_invoice")
    internal_id = payload.get("internal_id")
    if not internal_id:
        return {"ok": False, "error": "internal_id required"}

    if event == "posted":
        hub.confirm_posted(
            db, connector_name=connector_name,
            internal_entity=internal_entity, internal_id=str(internal_id),
            external_id=payload.get("external_id", ""),
        )
        if internal_entity == "vendor_invoice":
            ap_service.mark_posted(
                db, invoice_id=int(internal_id),
                actor=f"erp:{connector_name}",
                external_id=payload.get("external_id", ""),
            )
        return {"ok": True, "status": "POSTED"}

    return {"ok": False, "error": f"unsupported event: {event}"}
