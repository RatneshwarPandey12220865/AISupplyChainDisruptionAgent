"""1. Detection agent.

`detect_anomalies` is the deterministic rules engine run against every bus event (stock thresholds,
lead-time jumps, ETA slips, sensor excursions, external regional risk). `detection_node` is the first
LangGraph node: it de-duplicates, enriches with external signals, asks DeepSeek to triage the anomaly
and opens a Disruption record.
"""
from typing import Literal

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.common import active_signals, days_until, record, regional_risk
from app.agents.state import AgentState
from app.db import session_scope
from app.llm import structured_call
from app.models import (Disruption, InventoryLevel, Product, Shipment, Supplier, SupplierProduct, Warehouse,
                        utcnow)

TERMINAL_STATUSES = ("resolved", "rejected", "closed", "failed")
OUTAGE_DAYS = {"force_majeure": 28, "shutdown": 21, "capacity_reduced": 10}
SENSOR_LIMITS = {"temperature_c": 35.0, "shock_g": 6.0, "humidity_pct": 85.0}
SEVERITY_ORDER = ["low", "medium", "high", "critical"]


# --------------------------------------------------------------------------- rules engine
def _on_hand(session: Session, product_id: int, warehouse_id: int) -> float:
    return float(session.scalar(select(InventoryLevel.on_hand).where(
        InventoryLevel.product_id == product_id, InventoryLevel.warehouse_id == warehouse_id)) or 0)


def _most_exposed_warehouse(session: Session, product: Product) -> int:
    rows = session.execute(select(InventoryLevel.warehouse_id, InventoryLevel.on_hand)
                           .where(InventoryLevel.product_id == product.id)
                           .order_by(InventoryLevel.on_hand)).first()
    return rows[0] if rows else session.scalar(select(func.min(Warehouse.id)))


def _rule_severity(on_hand: float, demand: float, arrival_day: float) -> str:
    cover = on_hand / max(demand, 1e-9)
    gap = arrival_day - cover
    if gap > 7:
        return "critical"
    if gap > 2:
        return "high"
    if gap > 0:
        return "medium"
    return "low"


def _anomaly(session: Session, type_: str, product: Product, supplier_id: int | None, warehouse_id: int,
             base_arrival_days: float, delay_days: float, affected_qty: float, description: str,
             shipment_id: int | None = None, **evidence) -> dict:
    on_hand = _on_hand(session, product.id, warehouse_id)
    return {
        "type": type_, "product_id": product.id, "sku": product.sku, "supplier_id": supplier_id,
        "warehouse_id": warehouse_id, "shipment_id": shipment_id,
        "base_arrival_days": round(base_arrival_days, 2), "delay_days": round(delay_days, 2),
        "affected_qty": round(affected_qty), "on_hand": on_hand, "description": description,
        "rule_severity": _rule_severity(on_hand, product.daily_demand_mean, base_arrival_days + delay_days),
        "evidence": evidence,
    }


def _lead_time(session: Session, supplier_id: int | None, product_id: int) -> float:
    lt = session.scalar(select(SupplierProduct.lead_time_days).where(
        SupplierProduct.supplier_id == supplier_id, SupplierProduct.product_id == product_id))
    return float(lt or 14)


def detect_anomalies(session: Session, event: dict) -> list[dict]:
    t, p = event["type"], event["payload"]
    found: list[dict] = []

    if t == "inventory.update":
        product = session.get(Product, p["product_id"])
        wh_id = p["warehouse_id"]
        lead = _lead_time(session, product.primary_supplier_id, product.id)
        inbound = session.scalar(select(func.coalesce(func.sum(Shipment.quantity), 0)).where(
            Shipment.product_id == product.id, Shipment.warehouse_id == wh_id,
            Shipment.status.in_(("in_transit", "delayed")))) or 0
        need = product.daily_demand_mean * lead + product.safety_stock
        on_hand = float(p["on_hand"])
        if on_hand < product.reorder_point and on_hand + inbound < need:
            found.append(_anomaly(
                session, "low_stock", product, product.primary_supplier_id, wh_id,
                base_arrival_days=lead, delay_days=0, affected_qty=product.daily_demand_mean * lead,
                description=f"{product.sku} on-hand {on_hand:.0f} below reorder point {product.reorder_point:.0f}"
                            f" with only {inbound:.0f} inbound (needs {need:.0f} to cover {lead:.0f}-day lead time)",
                reorder_point=product.reorder_point, inbound=float(inbound)))

    elif t == "supplier.update":
        supplier = session.get(Supplier, p["supplier_id"])
        prev, new = p.get("previous_lead_time_days"), p.get("lead_time_days")
        if prev is not None and new is not None and new - prev >= 2 and (new - prev) / prev >= 0.2:
            product = session.get(Product, p["product_id"])
            found.append(_anomaly(
                session, "lead_time_increase", product, supplier.id, _most_exposed_warehouse(session, product),
                base_arrival_days=prev, delay_days=new - prev, affected_qty=product.daily_demand_mean * prev,
                description=f"{supplier.name} raised {product.sku} lead time {prev:.0f}d -> {new:.0f}d. "
                            f"{p.get('message', '')}".strip(),
                previous_lead_time=prev, new_lead_time=new))
        if p.get("status") in OUTAGE_DAYS and p.get("previous_status") != p["status"]:
            products = session.scalars(select(Product).where(Product.primary_supplier_id == supplier.id)).all()
            for product in products:
                lead = _lead_time(session, supplier.id, product.id)
                found.append(_anomaly(
                    session, "supplier_outage", product, supplier.id, _most_exposed_warehouse(session, product),
                    base_arrival_days=lead, delay_days=OUTAGE_DAYS[p["status"]],
                    affected_qty=product.daily_demand_mean * lead,
                    description=f"{supplier.name} reported {p['status'].replace('_', ' ')}. {p.get('message', '')}".strip(),
                    supplier_status=p["status"]))

    elif t == "shipment.eta_update":
        sh = session.get(Shipment, p["shipment_id"])
        delay = (sh.eta - sh.original_eta).total_seconds() / 86400
        if delay >= 1:
            found.append(_anomaly(
                session, "shipment_delay", sh.product, sh.supplier_id, sh.warehouse_id,
                base_arrival_days=days_until(sh.original_eta), delay_days=delay, affected_qty=sh.quantity,
                shipment_id=sh.id,
                description=f"Shipment {sh.code} ({sh.quantity:.0f} x {sh.product.sku}, {sh.carrier} {sh.vessel})"
                            f" delayed {delay:.1f} days. {p.get('reason', '')}".strip(),
                shipment_code=sh.code, new_eta=sh.eta.isoformat()))

    elif t == "telemetry.reading":
        limit = SENSOR_LIMITS.get(p["metric"])
        if limit is not None and float(p["value"]) > limit and p.get("shipment_code"):
            sh = session.scalar(select(Shipment).where(Shipment.code == p["shipment_code"]))
            if sh:
                replacement = _lead_time(session, sh.supplier_id, sh.product_id)
                found.append(_anomaly(
                    session, "cargo_damage_risk", sh.product, sh.supplier_id, sh.warehouse_id,
                    base_arrival_days=days_until(sh.eta), delay_days=replacement, affected_qty=sh.quantity,
                    shipment_id=sh.id,
                    description=f"{p['metric']}={p['value']} exceeds limit {limit} on shipment {sh.code}; "
                                f"cargo may need replacement ({replacement:.0f}-day lead time)",
                    metric=p["metric"], value=p["value"], limit=limit))

    elif t == "external.signal":
        severity = float(p["severity"])
        if severity >= 0.3:
            shipments = session.scalars(
                select(Shipment).join(Supplier, Shipment.supplier_id == Supplier.id).where(
                    Shipment.status.in_(("in_transit", "delayed")),
                    (Shipment.route_region == p["region"]) | (Supplier.region == p["region"]))).all()
            shipments = sorted(shipments, key=lambda s: s.quantity * s.product.unit_price, reverse=True)[:5]
            for sh in shipments:
                found.append(_anomaly(
                    session, "external_risk", sh.product, sh.supplier_id, sh.warehouse_id,
                    base_arrival_days=days_until(sh.eta), delay_days=round(severity * 12, 1),
                    affected_qty=sh.quantity, shipment_id=sh.id,
                    description=f"{p['kind'].replace('_', ' ').title()} in {p['region']} (severity {severity:.2f}) "
                                f"threatens shipment {sh.code}: {p.get('description', '')}".strip(),
                    signal_id=p.get("signal_id"), signal_kind=p["kind"]))

    for a in found:
        a["event_id"] = event["event_id"]
        a["event_type"] = t
    return found


# --------------------------------------------------------------------------- LangGraph node
class Triage(BaseModel):
    severity: Literal["low", "medium", "high", "critical"]
    title: str = Field(description="Short headline, max 90 chars")
    summary: str = Field(description="2-3 sentences for a supply planner")
    root_cause: str
    confidence: float = Field(ge=0, le=1)


def detection_node(state: AgentState, config: RunnableConfig) -> dict:
    a = state["anomaly"]
    thread_id = config["configurable"]["thread_id"]
    with session_scope() as s:
        dup = s.scalar(select(Disruption.id).where(
            Disruption.type == a["type"], Disruption.product_id == a["product_id"],
            Disruption.supplier_id == a["supplier_id"], Disruption.warehouse_id == a["warehouse_id"],
            Disruption.status.not_in(TERMINAL_STATUSES)))
        if dup:
            record(s, dup, "detection", f"Duplicate signal from event {a['event_id'][:8]} merged")
            return {"duplicate_of": dup, "status": "duplicate"}

        product = s.get(Product, a["product_id"])
        supplier = s.get(Supplier, a["supplier_id"]) if a["supplier_id"] else None
        region = supplier.region if supplier else ""
        signals = [{"kind": x.kind, "severity": x.severity, "description": x.description}
                   for x in active_signals(s, region)] if region else []
        anomaly = {**a, "regional_risk": regional_risk(s, region) if region else 0.0, "external_signals": signals}

        def fallback() -> Triage:
            return Triage(severity=a["rule_severity"], title=f"{a['type'].replace('_', ' ').title()}: {product.sku}"
                          + (f" from {supplier.name}" if supplier else ""), summary=a["description"],
                          root_cause=a["type"].replace("_", " "), confidence=0.6)

        triage, source = structured_call(
            "You are the Detection agent of an autonomous supply-chain control tower. Triage the anomaly: "
            "classify severity (a stockout before replenishment is at least 'high'; regional external risk "
            "should raise severity), write a headline and a concise summary, identify the likely root cause.",
            {"anomaly": anomaly, "product": {"sku": product.sku, "name": product.name,
                                             "daily_demand": product.daily_demand_mean},
             "supplier": {"name": supplier.name, "region": region, "reliability": supplier.reliability_score}
             if supplier else None},
            Triage, fallback)
        # The LLM may escalate but never downgrade below the rules engine's severity.
        if SEVERITY_ORDER.index(triage.severity) < SEVERITY_ORDER.index(a["rule_severity"]):
            triage.severity = a["rule_severity"]

        d = Disruption(
            thread_id=thread_id, event_id=a["event_id"], type=a["type"], severity=triage.severity,
            status="analyzing", title=triage.title[:300], summary=triage.summary, product_id=a["product_id"],
            supplier_id=a["supplier_id"], warehouse_id=a["warehouse_id"], shipment_id=a.get("shipment_id"),
            anomaly=anomaly, triage={**triage.model_dump(), "source": source}, created_at=utcnow())
        s.add(d)
        s.flush()
        record(s, d.id, "detection", f"Anomaly triaged as {triage.severity} ({source})")
        return {"disruption_id": d.id, "anomaly": anomaly, "triage": {**triage.model_dump(), "source": source},
                "status": "analyzing"}
