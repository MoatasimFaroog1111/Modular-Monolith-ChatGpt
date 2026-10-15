"""ERP integration hub.

The application core never knows ERP model names. All ERP operations go
through the internal ERPConnector contract; each system gets its own
adapter. Every call carries an idempotency key; failures land in a review
queue instead of disappearing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class ERPMapping(Base):
    """internal_id ↔ external_id mapping for every posted object."""

    __tablename__ = "erp_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    connector_name: Mapped[str] = mapped_column(String(64), index=True)
    internal_entity: Mapped[str] = mapped_column(String(64), index=True)
    internal_id: Mapped[str] = mapped_column(String(64), index=True)
    external_model: Mapped[str] = mapped_column(String(64), default="")
    external_id: Mapped[str | None] = mapped_column(default=None, index=True)
    sync_status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(default=None)


class ERPConnector(ABC):
    """The stable internal contract (per blueprint)."""

    name: str = "abstract"

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def create_vendor_bill_draft(self, bundle: dict, idempotency_key: str) -> dict: ...

    @abstractmethod
    def get_payment_status(self, external_id: str) -> dict: ...

    # The full contract surface (implemented as needed by concrete adapters):
    # upsert_supplier, get_item_master, get_available_stock,
    # create_purchase_requisition, create_purchase_order, create_goods_receipt,
    # upload_attachment, create_payment_draft, get_accounting_dimensions,
    # reverse_or_cancel_document
