from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.audit.models import AuditEvent


def record(
    db: Session,
    *,
    actor: str,
    event: str,
    entity_type: str,
    entity_id,
    before: dict | None = None,
    after: dict | None = None,
    channel: str = "api",
    result: str = "ok",
) -> AuditEvent:
    """Append an audit event. Caller commits together with the business change."""
    ev = AuditEvent(
        actor=actor,
        event=event,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
        channel=channel,
        result=result,
    )
    db.add(ev)
    return ev


def timeline(db: Session, entity_type: str, entity_id) -> list[AuditEvent]:
    return (
        db.query(AuditEvent)
        .filter(
            AuditEvent.entity_type == entity_type,
            AuditEvent.entity_id == str(entity_id),
        )
        .order_by(AuditEvent.id)
        .all()
    )
