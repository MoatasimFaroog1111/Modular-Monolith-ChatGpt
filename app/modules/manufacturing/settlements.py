"""Approved invoice-price variance, allocated over accepted stock in cents.

Weighted-average policy. Taxes and charges are not silently capitalized.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, String, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.modules.accounts_payable.models import VendorInvoice
from app.modules.audit import service as audit
from app.modules.manufacturing.models import MaterialIssue
from app.modules.purchasing.models import PurchaseOrder
from app.modules.receiving.service import accepted_qty_by_item
from app.platform.database import Base, get_db
from app.platform.security import Role, User, current_user, require_roles
from app.shared.errors import Conflict, DomainError, NotFound, PermissionDenied

router = APIRouter(prefix="/api/v1", tags=["Material invoice settlements"])
READ = (Role.GM, Role.FINANCE, Role.ACCOUNTANT, Role.AUDITOR, Role.ENGINEER)
PREPARE = (Role.ACCOUNTANT, Role.FINANCE)
APPROVE = (Role.GM, Role.FINANCE)


def D(value):
    return Decimal(str(value))


class MaterialSettlement(Base):
    __tablename__ = "material_settlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(unique=True)
    purchase_order_id: Mapped[int] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    lines: Mapped[list] = mapped_column(JSON)
    created_by: Mapped[int] = mapped_column()
    approved_by: Mapped[int | None] = mapped_column(default=None)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(timezone.utc))
    approved_at: Mapped[datetime | None] = mapped_column(default=None)


class SettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invoice_id: int = Field(gt=0)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(min_length=3, max_length=512)


def cents(value) -> int:
    return int((D(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def item_key(line):
    return line.get("item_code") or line.get("description", "")


def lock_po(db, po_id):
    db.execute(update(PurchaseOrder).where(PurchaseOrder.id == po_id)
               .values(status=PurchaseOrder.status))
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound("أمر الشراء غير موجود")
    return po


def settlement_lines(db, invoice):
    if invoice.status not in ("READY_TO_POST", "EXPORTED", "POSTED_TO_ERP", "PAID_OR_RECONCILED"):
        raise DomainError("التسوية تتطلب فاتورة مطابقة أو استثناء معتمداً")
    po = db.get(PurchaseOrder, invoice.purchase_order_id)
    if po is None:
        raise DomainError("الفاتورة غير مرتبطة بأمر شراء")
    if invoice.currency != "SAR" or po.currency != "SAR":
        raise DomainError("التسوية الحالية تدعم SAR فقط دون افتراض سعر صرف")
    if invoice.supplier != po.supplier:
        raise DomainError("مورد الفاتورة لا يطابق أمر الشراء")
    accepted = accepted_qty_by_item(po.id, db)
    po_lines = {item_key(l): l for l in po.lines}
    if len(po_lines) != len(po.lines):
        raise DomainError("بنود PO مكررة؛ يلزم رمز مادة مميز قبل التسوية")
    prior = {}
    for record in db.query(MaterialSettlement).filter_by(
        purchase_order_id=po.id, status="APPROVED"
    ).all():
        if record.invoice_id == invoice.id:
            continue
        for line in record.lines:
            prior[line["item_key"]] = prior.get(line["item_key"], Decimal(0)) + D(line["quantity"])
    lines, seen = [], set()
    for source in invoice.lines:
        key = item_key(source)
        target = po_lines.get(key)
        if not key or key in seen or target is None:
            raise DomainError("بنود الفاتورة مكررة أو غير موجودة بأمر الشراء")
        seen.add(key)
        qty, price, po_price = (D(source.get("quantity", 0)),
                                D(source.get("unit_price", 0)), D(target["unit_price"]))
        if (not all(v.is_finite() for v in (qty, price, po_price))
                or qty <= 0 or price < 0 or po_price < 0):
            raise DomainError("كمية أو سعر غير صالح للتسوية")
        if source.get("uom", "") != target.get("uom", ""):
            raise DomainError("وحدة قياس الفاتورة لا تطابق أمر الشراء")
        received = D(accepted.get(key, 0))
        if qty + prior.get(key, 0) > received:
            raise DomainError("كميات التسوية تتجاوز المقبول في الاستلام")
        lines.append({"item_key": key, "quantity": str(qty),
                      "po_unit_price": str(po_price), "invoice_unit_price": str(price),
                      "variance_cents": cents(qty * (price - po_price)),
                      "uom": source.get("uom", "")})
    if not lines:
        raise DomainError("الفاتورة لا تحتوي بنود تسوية")
    return lines


def serialize(record):
    return {"id": record.id, "invoice_id": record.invoice_id,
            "purchase_order_id": record.purchase_order_id, "status": record.status,
            "lines": record.lines, "variance_cents": sum(l["variance_cents"] for l in record.lines),
            "created_by": record.created_by, "approved_by": record.approved_by, "note": record.note}


@router.get("/material-settlements", dependencies=[Depends(require_roles(*READ))])
def list_settlements(db: Session = Depends(get_db)):
    return [serialize(s) for s in db.query(MaterialSettlement).order_by(MaterialSettlement.id).all()]


@router.get("/material-settlements/candidates", dependencies=[Depends(require_roles(*READ))])
def candidates(db: Session = Depends(get_db)):
    existing = {s.invoice_id for s in db.query(MaterialSettlement).all()}
    return [{"id": i.id, "invoice_no": i.invoice_no, "supplier": i.supplier,
             "purchase_order_id": i.purchase_order_id, "status": i.status}
            for i in db.query(VendorInvoice).order_by(VendorInvoice.id).all()
            if i.id not in existing and i.status in
            ("READY_TO_POST", "EXPORTED", "POSTED_TO_ERP", "PAID_OR_RECONCILED")]


@router.post("/material-settlements", status_code=201,
             dependencies=[Depends(require_roles(*PREPARE))])
def create_settlement(body: SettlementInput, db: Session = Depends(get_db),
                      actor: User = Depends(current_user)):
    inv = db.get(VendorInvoice, body.invoice_id)
    if inv is None:
        raise NotFound("فاتورة المورد غير موجودة")
    lock_po(db, inv.purchase_order_id)
    from app.modules.manufacturing.financial_guards import ensure_po_open
    ensure_po_open(db, inv.purchase_order_id)
    if db.query(MaterialSettlement).filter_by(invoice_id=inv.id).first():
        raise Conflict("توجد تسوية لهذه الفاتورة؛ لا يمكن تحميلها مرتين")
    record = MaterialSettlement(invoice_id=inv.id, purchase_order_id=inv.purchase_order_id,
                                lines=settlement_lines(db, inv), created_by=actor.id)
    db.add(record)
    db.flush()
    audit.record(db, actor=actor.username, event="settlement.created",
                 entity_type="material_settlement", entity_id=record.id, after=serialize(record))
    db.commit()
    return serialize(record)


@router.post("/material-settlements/{settlement_id}/approve",
             dependencies=[Depends(require_roles(*APPROVE))])
def approve_settlement(settlement_id: int, body: ApprovalInput,
                       db: Session = Depends(get_db), actor: User = Depends(current_user)):
    rec = db.get(MaterialSettlement, settlement_id)
    if rec is None:
        raise NotFound("التسوية غير موجودة")
    lock_po(db, rec.purchase_order_id)
    from app.modules.manufacturing.financial_guards import ensure_po_open
    ensure_po_open(db, rec.purchase_order_id)
    db.refresh(rec)
    if rec.status != "DRAFT":
        raise Conflict("التسوية معتمدة مسبقاً")
    if rec.created_by == actor.id:
        raise PermissionDenied("لا يجوز لمعد التسوية اعتمادها")
    if not body.note.strip():
        raise DomainError("ملاحظة الاعتماد مطلوبة")
    inv = db.get(VendorInvoice, rec.invoice_id)
    if inv is None or settlement_lines(db, inv) != rec.lines:
        raise Conflict("تغير مصدر الفاتورة؛ راجع التسوية قبل اعتمادها")
    rec.status, rec.approved_by, rec.note = "APPROVED", actor.id, body.note.strip()
    rec.approved_at = datetime.now(timezone.utc)
    audit.record(db, actor=actor.username, event="settlement.approved",
                 entity_type="material_settlement", entity_id=rec.id, after=serialize(rec))
    db.commit()
    return serialize(rec)


def allocate_cents(total, quantities):
    """Largest remainder; signed adjustments conserve cents, incl. inventory."""
    denominator = sum(quantities.values(), Decimal(0))
    if not denominator:
        return {key: 0 for key in quantities}
    raw = {k: abs(total) * q / denominator for k, q in quantities.items()}
    allocated = {k: int(v) for k, v in raw.items()}
    order = sorted(raw, key=lambda k: (-(raw[k] - allocated[k]), k))
    for k in order[:abs(total) - sum(allocated.values())]:
        allocated[k] += 1
    return {k: v * (-1 if total < 0 else 1) for k, v in allocated.items()}


def material_adjustments(db, unit_id):
    issues = db.query(MaterialIssue).order_by(MaterialIssue.id).all()
    targets = {(l["purchase_order_id"], item_key(l))
               for issue in issues if issue.unit_id == unit_id for l in issue.lines}
    total, sources, fully_settled = 0, [], True
    for po_id, key in sorted(targets):
        accepted = D(accepted_qty_by_item(po_id, db).get(key, 0))
        qty_by_unit = {}
        for issue in issues:
            for line in issue.lines:
                if line.get("purchase_order_id") == po_id and item_key(line) == key:
                    group = str(issue.unit_id).zfill(12)
                    qty_by_unit[group] = qty_by_unit.get(group, Decimal(0)) + D(line["quantity"])
        remaining = accepted - sum(qty_by_unit.values(), Decimal(0))
        if remaining < 0:
            raise DomainError("رصيد الصرف يتجاوز الاستلام؛ يلزم تصحيح قبل التسوية")
        qty_by_unit["inventory"] = remaining
        billed, variance, ids = Decimal(0), 0, []
        for rec in db.query(MaterialSettlement).filter_by(
            purchase_order_id=po_id, status="APPROVED"
        ).all():
            for line in rec.lines:
                if line["item_key"] == key:
                    billed += D(line["quantity"])
                    variance += line["variance_cents"]
                    ids.append(rec.id)
        # Preserve the exact cents frozen on closed units when inventory is
        # later issued to other units. The open pool receives the remainder.
        from app.modules.manufacturing.financial_close import FinancialClose
        fixed = {}
        for closed in db.query(FinancialClose).filter_by(status="CLOSED").all():
            group = str(closed.unit_id).zfill(12)
            if group not in qty_by_unit:
                continue
            for src in closed.snapshot.get("material_settlement_sources", []):
                if src["purchase_order_id"] == po_id and src["item_key"] == key:
                    fixed[group] = src["unit_variance_cents"]
        allocated = {**allocate_cents(
            variance - sum(fixed.values()),
            {k: q for k, q in qty_by_unit.items() if k not in fixed},
        ), **fixed}
        share = allocated.get(str(unit_id).zfill(12), 0)
        total += share
        complete = accepted > 0 and billed == accepted
        fully_settled &= complete
        sources.append({"purchase_order_id": po_id, "item_key": key,
                        "accepted_quantity": str(accepted), "settled_quantity": str(billed),
                        "remaining_inventory_quantity": str(remaining),
                        "total_variance_cents": variance, "unit_variance_cents": share,
                        "inventory_variance_cents": allocated["inventory"],
                        "settlement_ids": ids, "fully_settled": complete})
    return {"adjustment_cents": total, "fully_settled": fully_settled, "sources": sources}
