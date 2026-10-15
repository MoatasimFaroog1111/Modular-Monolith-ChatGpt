"""Intake service: store the raw message, classify, draft a request.

The default extractor is deliberately conservative: it copies the raw text
into the draft and marks everything else for human completion. A production
AI adapter may prefill fields, but the mandatory-field gate in the
material-requests service still decides whether the draft is complete.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.intake.models import InboundMessage, MaterialRequestExtractor
from app.shared.errors import DomainError


class ConservativeExtractor(MaterialRequestExtractor):
    """No-fabrication fallback. Real AI providers implement the same port."""

    def extract(self, text: str) -> dict:
        return {
            "classification": "material_request" if text else "other",
            "confidence": 0.0,
            "draft": {"raw_message": text},
        }


_extractor = ConservativeExtractor()


def set_extractor(extractor: MaterialRequestExtractor) -> None:
    global _extractor
    _extractor = extractor


def ingest_message(
    db: Session, *, channel: str, sender: str, text: str,
    media_type: str | None = None, create_draft_fn=None,
) -> tuple[InboundMessage, object | None]:
    """Store the message and optionally create a linked draft request.

    create_draft_fn(db, message) is injected by the router to avoid a
    circular import; it creates the DRAFT material request and returns it.
    """
    if not text and not media_type:
        raise DomainError("رسالة فارغة")

    result = _extractor.extract(text)
    msg = InboundMessage(
        channel=channel,
        sender=sender,
        body_text=text,
        media_type=media_type,
        ai_classification=result.get("classification"),
        ai_confidence=result.get("confidence"),
        draft=result.get("draft"),
    )
    db.add(msg)
    db.flush()

    mr = None
    if create_draft_fn and result.get("classification") == "material_request":
        mr = create_draft_fn(db, msg)
        msg.processed_at = datetime.now(timezone.utc)

    audit.record(
        db, actor=sender, event="message.ingested",
        entity_type="inbound_message", entity_id=msg.id, channel=channel,
        after={"classification": msg.ai_classification,
               "confidence": msg.ai_confidence},
    )
    db.commit()
    return msg, mr
