"""Transaction locks and close guards shared by cost-producing commands."""
from app.shared.errors import Conflict


def ensure_unit_open(db, unit_id):
    from app.modules.manufacturing.expenses import lock_unit
    from app.modules.manufacturing.financial_close import active_close
    lock_unit(db, unit_id)
    if active_close(db, unit_id):
        raise Conflict("الوحدة مقفلة مالياً؛ يلزم إعادة فتح مخولة قبل التعديل")


def ensure_po_open(db, po_id):
    from app.modules.manufacturing.models import MaterialIssue
    from app.modules.manufacturing.settlements import lock_po
    from app.modules.material_requests.models import MaterialRequest
    po = lock_po(db, po_id)
    ids = {issue.unit_id for issue in db.query(MaterialIssue).all()
           if any(line.get("purchase_order_id") == po_id for line in issue.lines)}
    request = db.get(MaterialRequest, po.material_request_id)
    if request and request.production_unit_id:
        ids.add(request.production_unit_id)
    for uid in sorted(ids):
        ensure_unit_open(db, uid)
