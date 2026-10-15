"""Safe export connector.

Used when no live ERP is integrated yet. It produces an importable
JSON/CSV bundle and records the mapping as EXPORTED — never Posted.
`Posted` requires a real confirmation from the financial system.
"""
from __future__ import annotations

import csv
import io
import json

from app.modules.integrations.base import ERPConnector


class ExportBundleConnector(ERPConnector):
    name = "export"

    def health_check(self) -> bool:
        return True

    def create_vendor_bill_draft(self, bundle: dict, idempotency_key: str) -> dict:
        json_bundle = json.dumps(bundle, ensure_ascii=False, indent=2)
        lines_csv = io.StringIO()
        writer = csv.writer(lines_csv)
        writer.writerow(["item_code", "description", "quantity", "uom", "unit_price"])
        for ln in bundle.get("lines", []):
            writer.writerow([
                ln.get("item_code", ""), ln.get("description", ""),
                ln.get("quantity", ""), ln.get("uom", ""), ln.get("unit_price", ""),
            ])
        return {
            "sync_status": "EXPORTED",
            "external_id": None,
            "idempotency_key": idempotency_key,
            "bundle_json": json_bundle,
            "lines_csv": lines_csv.getvalue(),
        }

    def get_payment_status(self, external_id: str) -> dict:
        return {"status": "NOT_INTEGRATED", "note": "Export connector has no live ERP"}
