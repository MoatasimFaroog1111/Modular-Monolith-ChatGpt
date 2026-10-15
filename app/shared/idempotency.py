"""Idempotency middleware for financial / webhook endpoints.

Rules (per blueprint):
- every financial command carries an Idempotency-Key header
- same key + same payload hash  -> replay the stored response (no duplicate)
- same key + different payload   -> 409 conflict (rejected)
- responses are stored in the same transaction as the business change
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import JSON, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base
from app.shared.errors import Conflict


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    response_body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


def hash_payload(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def begin_idempotent(db, request: Request) -> tuple[tuple[IdempotencyRecord, bool], None]:
    """Return ((record, is_replay)) when a key is present, else None.

    is_replay=True means the command already ran — return the stored
    response. is_replay=False means this is the first execution — the
    caller must store the response via finish_idempotent()."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None
    body = await request.body()
    # hash covers the body and the query string so the same key with a
    # different command is rejected
    material = body + b"?" + request.url.query.encode()
    request_hash = hashlib.sha256(material).hexdigest()

    existing = db.query(IdempotencyRecord).filter_by(key=key).first()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise Conflict(
                "Idempotency-Key مستخدم بمحتوى مختلف — رفض الطلب منعاً للتعارض"
            )
        return existing, True
    rec = IdempotencyRecord(key=key, request_hash=request_hash)
    db.add(rec)
    db.flush()
    return rec, False


def finish_idempotent(db, rec: IdempotencyRecord | None, status_code: int, body: str) -> None:
    if rec is None:
        return
    rec.status_code = status_code
    rec.response_body = body
    db.flush()
