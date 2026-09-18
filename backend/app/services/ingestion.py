"""Applies incoming events to the system of record before they are published to the event bus.

Each handler validates references, updates the relevant tables and enriches the payload with the
"before" values the Detection agent needs (e.g. the previous lead time).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (CustomerOrder, EventLog, ExternalSignal, InventoryLevel, Product, Shipment,
                        Supplier, SupplierProduct, TelemetryReading, Warehouse, utcnow)


class IngestError(ValueError):
    pass


def _product(session: Session, sku: str) -> Product:
    p = session.scalar(select(Product).where(Product.sku == sku))
    if not p:
        raise IngestError(f"Unknown SKU {sku!r}")
    return p


def _warehouse(session: Session, code: str) -> Warehouse:
    w = session.scalar(select(Warehouse).where(Warehouse.code == code))
    if not w:
        raise IngestError(f"Unknown warehouse {code!r}")
    return w


def _supplier(session: Session, payload: dict) -> Supplier:
    s = None
    if payload.get("supplier_id"):
        s = session.get(Supplier, int(payload["supplier_id"]))
    elif payload.get("supplier_name"):
        s = session.scalar(select(Supplier).where(Supplier.name == payload["supplier_name"]))
    if not s:
        raise IngestError("Unknown supplier")
    return s


def _parse_dt(value) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def apply_event(session: Session, event: dict) -> dict:
    t, p = event["type"], event["payload"]

    if t == "inventory.update":
        product, wh = _product(session, p["sku"]), _warehouse(session, p["warehouse"])
        inv = session.scalar(select(InventoryLevel).where(InventoryLevel.product_id == product.id,
                                                          InventoryLevel.warehouse_id == wh.id))
        if inv is None:
            inv = InventoryLevel(product_id=product.id, warehouse_id=wh.id, on_hand=0)
            session.add(inv)
        p["previous_on_hand"] = inv.on_hand
        inv.on_hand = float(p["on_hand"])
        p.update(product_id=product.id, warehouse_id=wh.id)

    elif t == "supplier.update":
        supplier = _supplier(session, p)
        p["supplier_id"] = supplier.id
        if p.get("status"):
            p["previous_status"] = supplier.status
            supplier.status = p["status"]
        if p.get("sku") and p.get("lead_time_days") is not None:
            product = _product(session, p["sku"])
            sp = session.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id == supplier.id,
                                                              SupplierProduct.product_id == product.id))
            if not sp:
                raise IngestError(f"{supplier.name} does not supply {product.sku}")
            p["previous_lead_time_days"] = sp.lead_time_days
            p["product_id"] = product.id
            sp.lead_time_days = float(p["lead_time_days"])

    elif t == "shipment.eta_update":
        sh = session.scalar(select(Shipment).where(Shipment.code == p["shipment_code"]))
        if not sh:
            raise IngestError(f"Unknown shipment {p['shipment_code']!r}")
        p["previous_eta"] = sh.eta.isoformat()
        if p.get("new_eta"):
            sh.eta = _parse_dt(p["new_eta"])
        elif p.get("delay_days") is not None:
            sh.eta = sh.eta + timedelta(days=float(p["delay_days"]))
        else:
            raise IngestError("Provide new_eta or delay_days")
        sh.status = "delayed" if sh.eta > sh.original_eta else "in_transit"
        p.update(shipment_id=sh.id, new_eta=sh.eta.isoformat())

    elif t == "telemetry.reading":
        reading = TelemetryReading(
            ts=_parse_dt(p["ts"]) if p.get("ts") else utcnow(), shipment_code=p.get("shipment_code", ""),
            metric=p["metric"], value=float(p["value"]), lat=p.get("lat"), lon=p.get("lon"))
        session.add(reading)

    elif t == "external.signal":
        hours = float(p.get("duration_hours", 72))
        sig = ExternalSignal(kind=p["kind"], region=p["region"], severity=float(p["severity"]),
                             description=p.get("description", ""), source=event.get("source", "api"),
                             expires_at=utcnow() + timedelta(hours=hours))
        session.add(sig)
        session.flush()
        p["signal_id"] = sig.id

    elif t == "order.created":
        product, wh = _product(session, p["sku"]), _warehouse(session, p["warehouse"])
        order = CustomerOrder(
            order_number=p["order_number"], customer=p["customer"], is_vip=bool(p.get("is_vip")),
            product_id=product.id, warehouse_id=wh.id, quantity=float(p["quantity"]),
            unit_price=float(p.get("unit_price", product.unit_price)), due_date=_parse_dt(p["due_date"]),
            penalty_per_day_late=float(p.get("penalty_per_day_late", 0)))
        session.add(order)
        p.update(product_id=product.id, warehouse_id=wh.id)

    else:
        raise IngestError(f"Unsupported event type {t!r}")

    session.add(EventLog(event_id=event["event_id"], type=t, source=event.get("source", ""), payload=p))
    return event
