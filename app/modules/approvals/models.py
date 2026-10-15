"""Version-bound approval records.

A regulatory-grade approval must capture: who approved, when, the exact
quotation version, the amount, the supplier and the lines. A free-text
"موافق" message is never sufficient — the decision binds to a snapshot hash.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)   # material_request
    entity_id: Mapped[int] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(16))   # APPROVED | REJECTED | MODIFY
    actor_id: Mapped[int] = mapped_column(index=True)  # approving user id
    actor_username: Mapped[str] = mapped_column(String(64))
    quotation_id: Mapped[int | None] = mapped_column(default=None)
    quotation_version: Mapped[int | None] = mapped_column(default=None)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    amount: Mapped[float | None] = mapped_column(default=None)
    currency: Mapped[str | None] = mapped_column(default=None)
    supplier: Mapped[str | None] = mapped_column(String(128), default=None)
    comment: Mapped[str] = mapped_column(String(512), default="")
    exception_reason: Mapped[str | None] = mapped_column(String(512), default=None)
    decided_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
