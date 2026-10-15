"""Accounts-payable service: invoice intake, 3-way match, exception handling.

Three-way match rules (per blueprint):
- invoice qty <= accepted receipt qty - previously billed qty
- invoice unit price == PO price (within approved tolerance)
- currency / tax terms / supplier match the PO
- invoice_no is unique per supplier (duplicates rejected)
- lines + tax + charges == invoice total (within rounding tolerance)
- a bill never becomes READY_TO_POST without an accepted GRN, unless a
  documented exception is approved by an authorized role.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.modules.accounts_payable import models as ap
from app.modules.audit import service as audit
from app.modules.material_requests import service as mr_service
from app.modules.purchasing.models import PurchaseOrder
from app.modules.receiving import service as receiving
from app.platform.config import get_settings
from app.platform.security import Role, User
from app.shared.errors import Conflict, DomainError, NotFound, PermissionDenied
from app.workflow import states
from app.modules.manufacturing.financial_guards import ensure_po_open


def create_invoice(
    db: Session, *, payload: dict, actor: User, attachment_id: int | None = None,
) -> ap.VendorInvoice:
    supplier = payload.get("supplier") or ""
    invoice_no = payload.get("invoice_no") or ""
    if not supplier or not invoice_no:
        raise DomainError("المورد ورقم الفاتورة إلزاميان")

    duplicate = (
        db.query(ap.VendorInvoice)
        .filter(
            ap.VendorInvoice.supplier == supplier,
            ap.VendorInvoice.invoice_no == invoice_no,
        )
        .first()
    )
    if duplicate is not None:
        raise Conflict(
            "فاتورة مكررة: نفس المورد ورقم الفاتورة مسجلان سابقاً"
        )

    lines = payload.get("lines") or []
    if not lines:
        raise DomainError("بنود الفاتورة مطلوبة")

    po_id = payload.get("purchase_order_id")
    po = db.get(PurchaseOrder, po_id) if po_id else None
    if po is None:
        raise DomainError("الفاتورة غير مرتبطة بأمر شراء صالح (UNLINKED)")
    ensure_po_open(db, po.id)
    if db.query(ap.VendorInvoice).filter_by(supplier=supplier, invoice_no=invoice_no).first():
        raise Conflict("فاتورة مكررة: نفس المورد ورقم الفاتورة مسجلان سابقاً")
    prior_invoice = db.query(ap.VendorInvoice).filter_by(purchase_order_id=po.id).first()

    inv = ap.VendorInvoice(
        invoice_no=invoice_no,
        supplier=supplier,
        invoice_date=payload.get("invoice_date", ""),
        currency=payload.get("currency", po.currency),
        lines=lines,
        tax_amount=float(payload.get("tax_amount", 0)),
        charges=float(payload.get("charges", 0)),
        total=float(payload.get("total", 0)),
        purchase_order_id=po_id,
        status="EXTRACTED",
        attachment_id=attachment_id,
    )
    db.add(inv)
    db.flush()

    mr = mr_service.get_or_404(db, po.material_request_id)
    # A PO may have several partial invoices. Their individual match states
    # are authoritative; do not rewind an already exported parent request.
    if prior_invoice is None:
        states.assert_transition(mr.status, states.INVOICE_PENDING)
        mr.status = states.INVOICE_PENDING
    elif mr.status == states.CLOSED:
        raise Conflict("طلب المواد مغلق؛ لا تسجل عليه فاتورة إضافية")
    audit.record(
        db, actor=actor.username, event="invoice.registered",
        entity_type="vendor_invoice", entity_id=inv.id,
        after={"invoice_no": invoice_no, "supplier": supplier, "total": inv.total},
    )
    db.commit()
    return inv


def match_invoice(db: Session, *, invoice_id: int, actor: User) -> dict:
    """Run the 3-way match. AI never adjusts numbers to force a match."""
    inv = db.get(ap.VendorInvoice, invoice_id)
    if inv is None:
        raise NotFound(f"الفاتورة {invoice_id} غير موجودة")
    ensure_po_open(db, inv.purchase_order_id)
    if inv.status not in ("EXTRACTED", "MATCHING", "EXCEPTION"):
        raise DomainError("المطابقة تجري على فاتورة لم تُرحّل بعد")

    po = db.get(PurchaseOrder, inv.purchase_order_id)
    if po is None:
        return _set_result(db, inv, actor, ap.UNLINKED, 0.0, "لا يوجد PO صالح")

    settings = get_settings()
    exceptions: list[dict] = []

    # --- duplicate check ---------------------------------------------------
    dup = (
        db.query(ap.VendorInvoice)
        .filter(
            ap.VendorInvoice.supplier == inv.supplier,
            ap.VendorInvoice.invoice_no == inv.invoice_no,
            ap.VendorInvoice.id != inv.id,
        )
        .first()
    )
    if dup is not None:
        return _set_result(db, inv, actor, ap.DUPLICATE_INVOICE, 0.0,
                           "المورد ورقم الفاتورة مسجلان سابقاً")

    # --- currency / supplier consistency -----------------------------------
    if inv.currency != po.currency or inv.supplier != po.supplier:
        return _set_result(db, inv, actor, ap.TAX_EXCEPTION, 0.0,
                           "العملة أو المورد لا يطابقان أمر الشراء")

    # --- totals arithmetic --------------------------------------------------
    lines_total = round(sum(l["quantity"] * l["unit_price"] for l in inv.lines), 2)
    computed = round(lines_total + inv.tax_amount + inv.charges, 2)
    if abs(computed - inv.total) > settings.rounding_tolerance:
        exceptions.append({
            "type": ap.TAX_EXCEPTION,
            "difference": round(computed - inv.total, 2),
            "justification": "إجمالي البنود + الضريبة + الرسوم لا يساوي إجمالي الفاتورة",
        })

    # --- receipt coverage ----------------------------------------------------
    accepted = receiving.accepted_qty_by_item(po.id, db)
    if not accepted:
        return _set_result(db, inv, actor, ap.MISSING_RECEIPT, 0.0,
                           "لا يوجد استلام معتمد (GRN) لأمر الشراء")

    po_items = {l.get("item_code") or l.get("description", ""): l for l in po.lines}
    previously_billed = _billed_qty_by_item(db, po.id, exclude_invoice_id=inv.id)

    # --- per-line qty / price ----------------------------------------------
    for ln in inv.lines:
        key = ln.get("item_code") or ln.get("description", "")
        po_line = po_items.get(key)
        if po_line is None:
            exceptions.append({
                "type": ap.UNLINKED, "difference": 0.0,
                "justification": f"بند الفاتورة '{key}' غير موجود في أمر الشراء",
            })
            continue

        received_qty = accepted.get(key, 0.0)
        available = received_qty - previously_billed.get(key, 0.0)
        if ln["quantity"] > available + settings.rounding_tolerance:
            exceptions.append({
                "type": ap.QTY_EXCEPTION,
                "difference": round(ln["quantity"] - max(available, 0), 2),
                "justification": (
                    f"كمية الفاتورة تتجاوز المقبول في GRN "
                    f"(متاح للفوترة: {available})"
                ),
            })

        po_price = po_line["unit_price"]
        diff_pct = abs(ln["unit_price"] - po_price) / po_price * 100 if po_price else 0
        if diff_pct > settings.price_tolerance_pct:
            exceptions.append({
                "type": ap.PRICE_EXCEPTION,
                "difference": round(ln["unit_price"] - po_price, 2),
                "justification": (
                    f"سعر الفاتورة يختلف عن PO بنسبة {diff_pct:.2f}% "
                    f"(التسامح المسموح {settings.price_tolerance_pct}%)"
                ),
            })

    if exceptions:
        return _set_result(db, inv, actor, exceptions[0]["type"],
                           exceptions[0]["difference"],
                           exceptions[0]["justification"], exceptions)

    return _set_result(db, inv, actor, ap.MATCHED, 0.0, "مطابقة ثلاثية ناجحة")


def _billed_qty_by_item(db: Session, po_id: int, exclude_invoice_id: int) -> dict:
    """Quantities already billed on posted/ready invoices of this PO."""
    invoices = (
        db.query(ap.VendorInvoice)
        .filter(
            ap.VendorInvoice.purchase_order_id == po_id,
            ap.VendorInvoice.id != exclude_invoice_id,
            ap.VendorInvoice.status.in_(["READY_TO_POST", "EXPORTED", "POSTED_TO_ERP", "PAID_OR_RECONCILED"]),
        )
        .all()
    )
    totals: dict[str, float] = {}
    for inv in invoices:
        for ln in inv.lines:
            key = ln.get("item_code") or ln.get("description", "")
            totals[key] = totals.get(key, 0) + ln["quantity"]
    return totals


def _set_result(db, inv, actor, result_type, difference, justification,
                exceptions=None) -> dict:
    inv.match_result = {
        "type": result_type, "difference": difference,
        "justification": justification, "exceptions": exceptions or [],
    }
    audit.record(
        db, actor=actor.username, event="invoice.matched",
        entity_type="vendor_invoice", entity_id=inv.id,
        after={"result": result_type, "difference": difference},
        result="ok" if result_type == ap.MATCHED else "rejected",
    )

    mr = _mr_of_invoice(db, inv)
    if mr is not None and mr.status == states.INVOICE_PENDING:
        states.assert_transition(mr.status, states.MATCHING)
        mr.status = states.MATCHING
    if result_type == ap.MATCHED:
        if mr is not None and mr.status == states.MATCHING:
            states.assert_transition(mr.status, states.READY_TO_POST)
            mr.status = states.READY_TO_POST
            audit.record(
                db, actor=actor.username, event="status.changed",
                entity_type="material_request", entity_id=mr.id,
                before={"status": states.MATCHING},
                after={"status": states.READY_TO_POST},
            )
        inv.status = "READY_TO_POST"
    else:
        if mr is not None and mr.status == states.MATCHING:
            mr.status = states.EXCEPTION
        inv.status = "EXCEPTION"
        db.add(ap.ExceptionCase(
            invoice_id=inv.id, match_type=result_type,
            difference=difference, justification=justification,
            owner_id=actor.id,
        ))
    db.commit()
    return {"invoice_id": inv.id, "match_result": inv.match_result,
            "invoice_status": inv.status}


def _mr_of_invoice(db, inv):
    po = db.get(PurchaseOrder, inv.purchase_order_id)
    if po is None:
        return None
    return mr_service.get_or_404(db, po.material_request_id)


def approve_exception(db: Session, *, invoice_id: int, actor: User,
                      justification: str) -> dict:
    """Exception approval is human and role-bound; AI never approves."""
    inv = db.get(ap.VendorInvoice, invoice_id)
    if inv is None:
        raise NotFound(f"الفاتورة {invoice_id} غير موجودة")
    ensure_po_open(db, inv.purchase_order_id)
    if actor.role not in (Role.GM, Role.FINANCE):
        raise PermissionDenied("اعتماد الاستثناء للمدير العام/المالي فقط")
    if inv.status != "EXCEPTION":
        raise DomainError("الفاتورة ليست في حالة استثناء")
    if len(justification.strip()) < 3:
        raise DomainError("مبرر اعتماد الاستثناء مطلوب")

    case = (
        db.query(ap.ExceptionCase)
        .filter(ap.ExceptionCase.invoice_id == inv.id, ap.ExceptionCase.decision == "OPEN")
        .order_by(ap.ExceptionCase.id.desc())
        .first()
    )
    if case is None:
        raise DomainError("لا يوجد استثناء مفتوح لهذه الفاتورة")

    case.decision = "APPROVED"
    case.justification = justification
    case.decided_by = actor.id
    case.decided_at = datetime.now(timezone.utc)

    mr = _mr_of_invoice(db, inv)
    audit.record(
        db, actor=actor.username, event="exception.approved",
        entity_type="vendor_invoice", entity_id=inv.id,
        after={"justification": justification},
    )
    if mr is not None and mr.status == states.EXCEPTION:
        mr.status = states.READY_TO_POST
        audit.record(
            db, actor=actor.username, event="status.changed",
            entity_type="material_request", entity_id=mr.id,
            before={"status": states.EXCEPTION},
            after={"status": states.READY_TO_POST},
        )
    inv.status = "READY_TO_POST"
    db.commit()
    return {"invoice_id": inv.id, "status": inv.status}


def export_bundle(db: Session, *, invoice_id: int, actor: User) -> dict:
    """Build the ERP handoff bundle. `Exported` is NOT `Posted` — the request
    only reaches POSTED_TO_ERP after a real confirmation from the ERP side.
    An invoice in EXCEPTION cannot be exported until the exception is
    explicitly approved."""
    inv = db.get(ap.VendorInvoice, invoice_id)
    if inv is None:
        raise NotFound(f"الفاتورة {invoice_id} غير موجودة")
    if inv.status != "READY_TO_POST":
        raise DomainError(
            "التجهيز للترحيل يتطلب مطابقة ناجحة أو استثناء معتمداً — "
            "فاتورة في حالة استثناء غير معتمد لا تُصدَّر"
        )
    if actor.role not in (Role.ACCOUNTANT, Role.FINANCE):
        raise PermissionDenied("تجهيز الترحيل للمحاسب/المالي فقط")

    po = db.get(PurchaseOrder, inv.purchase_order_id)
    mr = _mr_of_invoice(db, inv)
    bundle = {
        "supplier": inv.supplier,
        "invoice_no": inv.invoice_no,
        "invoice_date": inv.invoice_date,
        "currency": inv.currency,
        "lines": inv.lines,
        "tax_amount": inv.tax_amount,
        "charges": inv.charges,
        "total": inv.total,
        "purchase_order_no": po.po_no if po else None,
        "material_request_no": mr.request_no if mr else None,
        "production_unit_id": mr.production_unit_id if mr else None,
        "budget_code": mr.budget_code if mr else None,
        "attachments": [],   # attachment refs are included by the integration hub
    }
    inv.status = "EXPORTED"
    audit.record(
        db, actor=actor.username, event="invoice.exported",
        entity_type="vendor_invoice", entity_id=inv.id,
        after={"bundle_hash": hash(str(sorted(bundle.keys())))},
    )
    db.commit()
    return {"invoice_id": inv.id, "status": inv.status, "bundle": bundle}


def mark_posted(db: Session, *, invoice_id: int, actor: str,
                external_id: str) -> dict:
    """Called by the ERP integration hub ONLY after real external confirmation.
    Requires a prior EXPORTED state — posting cannot be confirmed out of thin air."""
    inv = db.get(ap.VendorInvoice, invoice_id)
    if inv is None:
        raise NotFound(f"الفاتورة {invoice_id} غير موجودة")
    if inv.status != "EXPORTED":
        raise DomainError(
            "لا يُسجّل الترحيل إلا بعد تصدير فعلي — التأكيد المجهول مرفوض"
        )

    mr = _mr_of_invoice(db, inv)
    if mr is not None and mr.status == states.READY_TO_POST:
        states.assert_transition(mr.status, states.POSTED_TO_ERP)
        mr.status = states.POSTED_TO_ERP
        audit.record(
            db, actor=actor, event="status.changed",
            entity_type="material_request", entity_id=mr.id,
            before={"status": states.READY_TO_POST},
            after={"status": states.POSTED_TO_ERP, "external_id": external_id},
        )
    inv.status = "POSTED_TO_ERP"
    audit.record(
        db, actor=actor, event="invoice.posted_to_erp",
        entity_type="vendor_invoice", entity_id=inv.id,
        after={"external_id": external_id},
    )
    db.commit()
    return {"invoice_id": inv.id, "status": inv.status, "external_id": external_id}
