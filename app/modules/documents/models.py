"""Document store: versioned attachments with content hashes.

The original file is never overwritten; every new version is a new row.
The hash detects replacement of evidence after a decision.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str | None] = mapped_column(default=None, index=True)
    doc_type: Mapped[str] = mapped_column(String(32), default="evidence")
    # evidence | quotation | receipt | bank_slip | invoice | photo | other
    filename: Mapped[str] = mapped_column(String(255), default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(default=1)
    storage_ref: Mapped[str] = mapped_column(String(512), default="")
    meta: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )

    def bump_version_for(self, content: bytes) -> "Attachment":
        """Return a new version row for the same entity/document type."""
        return Attachment(
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            doc_type=self.doc_type,
            filename=self.filename,
            content_hash=hashlib.sha256(content).hexdigest(),
            version=self.version + 1,
            storage_ref=self.storage_ref,
        )


def register(db, *, entity_type: str, entity_id, doc_type: str, filename: str,
             content: bytes, storage_ref: str = "local") -> Attachment:
    att = Attachment(
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        doc_type=doc_type,
        filename=filename,
        content_hash=hashlib.sha256(content).hexdigest(),
        storage_ref=storage_ref,
    )
    db.add(att)
    db.flush()
    return att
