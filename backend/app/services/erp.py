"""ERP write-back adapter.

Purchase orders, expedite requests and transfers are persisted locally (the system of record for this
service) and, when ERP_WEBHOOK_URL is set, pushed to the external ERP whose reference is stored back.
"""
import logging
import uuid

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PurchaseOrder

log = logging.getLogger(__name__)


def _po_number(kind: str) -> str:
    prefix = {"purchase": "PO", "expedite": "EXP", "transfer": "TRF"}[kind]
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def create_order(session: Session, *, kind: str, product_id: int, quantity: float, unit_price: float,
                 total_cost: float, expected_days: float, disruption_id: int,
                 supplier_id: int | None = None, warehouse_id: int | None = None,
                 source_warehouse_id: int | None = None) -> PurchaseOrder:
    po = PurchaseOrder(
        po_number=_po_number(kind), kind=kind, supplier_id=supplier_id, product_id=product_id,
        warehouse_id=warehouse_id, source_warehouse_id=source_warehouse_id, quantity=round(quantity),
        unit_price=round(unit_price, 4), total_cost=round(total_cost, 2), expected_days=expected_days,
        disruption_id=disruption_id, status="submitted",
    )
    session.add(po)
    session.flush()

    if settings.erp_webhook_url:
        try:
            resp = httpx.post(settings.erp_webhook_url, timeout=15, json={
                "po_number": po.po_number, "kind": kind, "supplier_id": supplier_id, "product_id": product_id,
                "warehouse_id": warehouse_id, "source_warehouse_id": source_warehouse_id,
                "quantity": po.quantity, "unit_price": po.unit_price, "total_cost": po.total_cost,
            })
            resp.raise_for_status()
            po.erp_ref = str(resp.json().get("id", "")) if "json" in resp.headers.get("content-type", "") else ""
        except Exception as exc:
            log.warning("ERP webhook failed for %s: %s (kept locally)", po.po_number, exc)
    return po
