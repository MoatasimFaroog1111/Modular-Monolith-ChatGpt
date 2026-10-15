"""Intake: message → structured draft, through a replaceable AI port.

Principle from the blueprint: AI proposes, humans decide. The extractor
may prefill a draft, but it never invents item codes (an unmapped item is
flagged for procurement review) and it never commits anything.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), default="api")  # api | whatsapp
    sender: Mapped[str] = mapped_column(String(64), index=True)
    body_text: Mapped[str | None] = mapped_column(Text, default=None)
    media_type: Mapped[str | None] = mapped_column(String(32), default=None)
    ai_classification: Mapped[str | None] = mapped_column(String(32), default=None)
    # material_request | progress_event | other
    ai_confidence: Mapped[float | None] = mapped_column(default=None)
    draft: Mapped[dict | None] = mapped_column(JSON, default=None)
    processed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )


class MaterialRequestExtractor(ABC):
    """AI extraction port — replaceable provider (Whisper/OCR/LLM)."""

    @abstractmethod
    def extract(self, text: str) -> dict:
        """Return {'classification': ..., 'confidence': float,
        'draft': {...}}. Missing fields stay absent — never fabricated."""
