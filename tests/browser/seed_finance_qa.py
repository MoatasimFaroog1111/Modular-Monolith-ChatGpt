"""Seed ONLY a fresh, isolated development server on loopback port 8770."""
import httpx

from tests.test_material_settlements import prepared, invoice
from tests.test_manufacturing import _add_bom
from tests.test_financial_close import complete_unit


def main():
    with httpx.Client(base_url="http://127.0.0.1:8770", timeout=20) as client:
        response = client.get("/api/v1/production-units", headers={"X-User": "gm1"})
        response.raise_for_status()
        if response.json():
            raise RuntimeError("Refusing to seed: use a fresh isolated QA database with no units")
        uid, po = prepared(client)
        _add_bom(client, uid)
        inv = invoice(client, po)
        complete_unit(client, uid)
        print({"demo_unit_id": uid, "purchase_order_id": po["id"], "invoice_id": inv["id"]})


if __name__ == "__main__":
    main()
