"""Immutable audit trail.

Every state transition and business decision writes an AuditEvent in the
same transaction as the change itself. Rows are append-only by convention
and never updated/deleted by application code.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc), index=True
    )
    actor: Mapped[str] = mapped_column(String(64), index=True)      # username
    event: Mapped[str] = mapped_column(String(64), index=True)       # e.g. status.changed
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[int] = mapped_column(String(64), index=True)
    before: Mapped[dict | None] = mapped_column(JSON, default=None)
    after: Mapped[dict | None] = mapped_column(JSON, default=None)
    channel: Mapped[str] = mapped_column(String(32), default="api")  # api | whatsapp | erp
    result: Mapped[str] = mapped_column(String(32), default="ok")    # ok | rejected | failed
