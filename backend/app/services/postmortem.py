"""Automated post-mortem and supplier reliability scoring.

When every order placed for a disruption has been received (or a planner resolves it manually), actual
recovery is compared with the plan, supplier reliability scores are updated with an exponentially
weighted moving average, and DeepSeek writes a short lessons-learned summary. Updated scores feed the
next Sourcing decisions (option ranking, arrival variance) and the HITL reliability guardrail.
"""
import logging

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.common import record
from app.llm import structured_call
from app.models import (Disruption, InventoryLevel, PurchaseOrder, Shipment, Supplier, SupplierDelivery,
                        SupplierProduct, SupplierScoreHistory, utcnow)
from app.services.notifications import notify

log = logging.getLogger(__name__)

EWMA_ALPHA = 0.2
FAULT_WEIGHT = {"shipment_delay": 1.0, "lead_time_increase": 1.0, "supplier_outage": 1.0,
                "cargo_damage_risk": 0.7, "external_risk": 0.25, "low_stock": 0.0}


def _update_score(session: Session, supplier_id: int, performance: float, weight: float, reason: str,
                  disruption_id: int) -> dict | None:
    if weight <= 0:
        return None
    sup = session.get(Supplier, supplier_id)
    old = sup.reliability_score
    alpha = EWMA_ALPHA * weight
    sup.reliability_score = round(min(1.0, max(0.0, (1 - alpha) * old + alpha * performance)), 4)
    session.add(SupplierScoreHistory(supplier_id=supplier_id, old_score=old, new_score=sup.reliability_score,
                                     reason=reason, disruption_id=disruption_id))
    return {"supplier": sup.name, "old": old, "new": sup.reliability_score, "reason": reason}


def _add_stock(session: Session, product_id: int, warehouse_id: int, qty: float) -> None:
    inv = session.scalar(select(InventoryLevel).where(InventoryLevel.product_id == product_id,
                                                      InventoryLevel.warehouse_id == warehouse_id))
    if inv:
        inv.on_hand += qty
    else:
        session.add(InventoryLevel(product_id=product_id, warehouse_id=warehouse_id, on_hand=qty))


def receive_purchase_order(session: Session, po_id: int, actual_days: float | None = None) -> PurchaseOrder:
    po = session.get(PurchaseOrder, po_id)
    if po is None or po.status != "submitted":
        raise ValueError("Purchase order not found or already closed")
    po.status, po.received_at = "received", utcnow()
    po.actual_days = actual_days if actual_days is not None else round(
        (po.received_at - po.created_at).total_seconds() / 86400, 2)
    if po.kind in ("purchase", "transfer"):
        _add_stock(session, po.product_id, po.warehouse_id, po.quantity)
    elif po.kind == "expedite":
        d = session.get(Disruption, po.disruption_id)
        sh = session.get(Shipment, d.shipment_id) if d and d.shipment_id else None
        if sh and sh.status != "delivered":
            sh.status = "delivered"
            _add_stock(session, sh.product_id, sh.warehouse_id, sh.quantity)
    if po.kind == "purchase" and po.supplier_id:
        session.add(SupplierDelivery(supplier_id=po.supplier_id, product_id=po.product_id,
                                     promised_days=po.expected_days, actual_days=po.actual_days))
    session.flush()
    return po


class PostMortem(BaseModel):
    summary: str
    what_went_well: list[str]
    what_went_wrong: list[str]
    lessons: list[str]


def run_postmortem(session: Session, disruption_id: int, actual_recovery_days: float | None = None,
                   actual_loss: float | None = None) -> Disruption:
    d = session.get(Disruption, disruption_id)
    if d is None:
        raise ValueError("Disruption not found")
    if d.postmortem:
        return d
    pos = session.scalars(select(PurchaseOrder).where(PurchaseOrder.disruption_id == d.id)).all()
    received = [p for p in pos if p.status == "received"]
    if actual_recovery_days is None:
        actual_recovery_days = max((p.actual_days or 0 for p in received), default=d.expected_recovery_days or 0)
    option = next((o for o in d.options if o["id"] == d.execution.get("option_id")), d.recommendation.get("option", {}))
    expected_days = option.get("expected_recovery_day") or d.expected_recovery_days or 0

    score_changes = []
    a = d.anomaly
    if d.supplier_id:
        lead = session.scalar(select(SupplierProduct.lead_time_days).where(
            SupplierProduct.supplier_id == d.supplier_id, SupplierProduct.product_id == d.product_id)) or 14
        perf = max(0.0, 1 - a.get("delay_days", 0) / max(lead, 1))
        change = _update_score(session, d.supplier_id, perf, FAULT_WEIGHT.get(d.type, 0.5),
                               f"{d.type}: {a.get('delay_days', 0):.1f}d delay vs {lead:.0f}d lead time", d.id)
        if change:
            score_changes.append(change)
    for p in received:
        if p.kind == "purchase" and p.supplier_id and p.actual_days:
            perf = min(1.0, p.expected_days / max(p.actual_days, 0.01))
            score_changes.append(_update_score(
                session, p.supplier_id, perf, 1.0,
                f"Mitigation {p.po_number}: promised {p.expected_days:.1f}d, delivered {p.actual_days:.1f}d", d.id))

    facts = {"title": d.title, "type": d.type, "severity": d.severity,
             "expected_loss_no_action": d.simulation.get("expected_loss"),
             "chosen_option": option.get("label"), "plan_expected_loss": option.get("expected_loss"),
             "actual_loss": actual_loss, "expected_recovery_days": expected_days,
             "actual_recovery_days": actual_recovery_days, "orders": [
                 {"po": p.po_number, "kind": p.kind, "promised_days": p.expected_days, "actual_days": p.actual_days}
                 for p in pos], "supplier_score_changes": score_changes}

    def fallback() -> PostMortem:
        on_time = actual_recovery_days <= expected_days * 1.1 + 0.5
        return PostMortem(
            summary=f"Recovered in {actual_recovery_days:.1f} days vs {expected_days:.1f} planned "
                    f"using '{option.get('label', 'n/a')}'.",
            what_went_well=["Recovery within plan"] if on_time else [],
            what_went_wrong=[] if on_time else ["Recovery slower than simulated"],
            lessons=["Supplier reliability scores updated for future sourcing decisions"])

    pm, source = structured_call(
        "You are writing a blameless supply-chain disruption post-mortem. Compare planned and actual "
        "outcomes, be specific and brief, and derive actionable lessons.", facts, PostMortem, fallback)
    record(session, d.id, "postmortem", f"Post-mortem complete ({source}); {len(score_changes)} score updates",
           status="resolved", resolved_at=utcnow(), actual_recovery_days=actual_recovery_days,
           postmortem={**pm.model_dump(), "source": source, "expected_recovery_days": expected_days,
                       "actual_recovery_days": actual_recovery_days, "actual_loss": actual_loss,
                       "score_changes": score_changes})
    notify(session, f"Resolved: {d.title}", pm.summary, level="info", disruption_id=d.id)
    return d


def auto_resolve(session: Session) -> int:
    """Background job: resolve executed disruptions once all their orders have been received."""
    done = 0
    for d in session.scalars(select(Disruption).where(
            Disruption.status.in_(("auto_executed", "approved_executed")))).all():
        pos = session.scalars(select(PurchaseOrder).where(PurchaseOrder.disruption_id == d.id)).all()
        if pos and all(p.status == "received" for p in pos):
            run_postmortem(session, d.id)
            done += 1
    return done
