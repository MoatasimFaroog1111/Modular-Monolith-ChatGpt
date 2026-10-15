"""Integration hub: dispatch ERP calls through registered connectors.

Every outbound call is idempotent (keyed), recorded in ERPMapping, and
failures mark the mapping for review instead of losing the transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.audit import service as audit
from app.modules.integrations.base import ERPMapping
from app.modules.integrations.export_connector import ExportBundleConnector
from app.shared.errors import DomainError, NotFound
from app.shared.idempotency import hash_payload

_CONNECTORS: dict[str, type] = {}


def register(name: str, cls) -> None:
    _CONNECTORS[name] = cls


def get_connector(name: str):
    if name not in _CONNECTORS:
        raise NotFound(f"موصل ERP غير مسجل: {name}")
    return _CONNECTORS[name]()


register("export", ExportBundleConnector)


def post_vendor_bill(
    db: Session, *, connector_name: str, bundle: dict, internal_entity: str,
    internal_id: str, idempotency_key: str,
) -> dict:
    """Push a bill bundle to an ERP connector. The internal entity stays
    non-posted until a real external confirmation arrives."""
    connector = get_connector(connector_name)

    mapping = (
        db.query(ERPMapping)
        .filter(
            ERPMapping.connector_name == connector_name,
            ERPMapping.internal_entity == internal_entity,
            ERPMapping.internal_id == str(internal_id),
        )
        .first()
    )
    if mapping is None:
        mapping = ERPMapping(
            connector_name=connector_name,
            internal_entity=internal_entity,
            internal_id=str(internal_id),
            sync_status="PENDING",
        )
        db.add(mapping)

    import json

    payload_hash = hash_payload(
        json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()
    )

    if mapping.sync_status in ("EXPORTED", "POSTED") and mapping.payload_hash == payload_hash:
        # idempotent replay — do not push twice
        return {"sync_status": mapping.sync_status, "replayed": True}

    try:
        result = connector.create_vendor_bill_draft(bundle, idempotency_key)
    except Exception as exc:  # noqa: BLE001 — failures must never be lost
        mapping.sync_status = "FAILED"
        audit.record(
            db, actor="erp_hub", event="erp.sync.failed",
            entity_type=internal_entity, entity_id=internal_id,
            result="failed", after={"error": str(exc)},
        )
        db.commit()
        raise DomainError(f"فشل التكامل مع ERP — السجل محفوظ للمراجعة: {exc}")

    mapping.sync_status = result.get("sync_status", "EXPORTED")
    mapping.external_id = result.get("external_id")
    mapping.payload_hash = payload_hash
    mapping.last_synced_at = datetime.now(timezone.utc)
    audit.record(
        db, actor="erp_hub", event="erp.sync.exported",
        entity_type=internal_entity, entity_id=internal_id,
        after={"connector": connector_name, "status": mapping.sync_status},
    )
    db.commit()
    return result


def confirm_posted(
    db: Session, *, connector_name: str, internal_entity: str, internal_id: str,
    external_id: str,
) -> ERPMapping:
    """Webhook path: the ERP confirmed the posting. Only this flips status.
    The mapping is upserted so a confirmation that arrives without a prior
    export record is still registered."""
    mapping = (
        db.query(ERPMapping)
        .filter(
            ERPMapping.connector_name == connector_name,
            ERPMapping.internal_entity == internal_entity,
            ERPMapping.internal_id == str(internal_id),
        )
        .first()
    )
    if mapping is None:
        mapping = ERPMapping(
            connector_name=connector_name,
            internal_entity=internal_entity,
            internal_id=str(internal_id),
            sync_status="PENDING",
        )
        db.add(mapping)
    mapping.sync_status = "POSTED"
    mapping.external_id = external_id
    mapping.last_synced_at = datetime.now(timezone.utc)
    audit.record(
        db, actor="erp_hub", event="erp.posted",
        entity_type=internal_entity, entity_id=internal_id,
        after={"external_id": external_id},
    )
    db.commit()
    return mapping
