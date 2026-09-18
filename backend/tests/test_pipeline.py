"""End-to-end agent graph runs against the seeded demo data (deterministic fallback, no LLM)."""
import pytest
from sqlalchemy import select

from app.agents.graph import is_waiting, resume
from app.db import session_scope
from app.events import make_event
from app.models import Disruption, InventoryLevel, PurchaseOrder, Supplier, SupplierScoreHistory
from app.seed import SCENARIOS
from app.services.ingestion import IngestError, apply_event
from app.services.postmortem import auto_resolve, receive_purchase_order
from app.worker import process_event


def trigger(scenario: str) -> dict:
    type_, payload = SCENARIOS[scenario]
    event = make_event(type_, dict(payload))
    with session_scope() as s:
        apply_event(s, event)
    return process_event(event)


def disruption(did: int) -> Disruption:
    with session_scope() as s:
        return s.get(Disruption, did)


def test_low_stock_auto_executes_within_guardrails():
    result = trigger("low_stock")
    assert result["anomalies"] == 1
    run = result["runs"][0]
    assert run["status"] == "auto_executed" and not run["interrupted"]
    d = disruption(run["disruption_id"])
    assert d.execution["mode"] == "auto"
    assert d.execution["total_cost"] <= 2000
    assert [t["node"] for t in d.trace] == ["detection", "impact", "simulation", "sourcing", "guardrail", "execution"]


def test_high_cost_plan_halts_for_approval_then_executes_on_approve():
    run = trigger("shipment_delay")["runs"][0]
    assert run["status"] == "awaiting_approval" and run["interrupted"]
    d = disruption(run["disruption_id"])
    assert d.guardrail["route"] == "escalate" and d.guardrail["reasons"]
    assert is_waiting(d.thread_id)

    alt = next(o["id"] for o in d.options if o["kind"] == "purchase")
    out = resume(d.thread_id, {"approved": True, "option_id": alt, "reviewer": "alice"})
    assert out["status"] == "approved_executed"
    d = disruption(d.id)
    assert d.execution["option_id"] == alt and d.decision["reviewer"] == "alice"
    assert not is_waiting(d.thread_id)


def test_rejection_makes_no_erp_changes():
    run = trigger("supplier_lead_time")["runs"][0]
    d = disruption(run["disruption_id"])
    assert resume(d.thread_id, {"approved": False})["status"] == "rejected"
    with session_scope() as s:
        assert not s.scalars(select(PurchaseOrder).where(PurchaseOrder.disruption_id == d.id)).all()


def test_duplicate_signal_is_merged():
    first = trigger("low_stock")["runs"][0]
    # A second identical low-stock reading while the first disruption is still open
    event = make_event("inventory.update", {"sku": "CON-USBC", "warehouse": "DAL", "on_hand": 1100})
    with session_scope() as s:
        apply_event(s, event)
    second = process_event(event)["runs"][0]
    assert second["duplicate_of"] == first["disruption_id"]


def test_port_congestion_flags_multiple_shipments():
    result = trigger("port_congestion")
    assert result["anomalies"] >= 2
    for run in result["runs"]:
        assert disruption(run["disruption_id"]).anomaly["regional_risk"] > 0.5


def test_receipt_triggers_postmortem_and_supplier_scoring():
    run = trigger("low_stock")["runs"][0]
    with session_scope() as s:
        po = s.scalar(select(PurchaseOrder).where(PurchaseOrder.disruption_id == run["disruption_id"]))
        wh_before = s.scalar(select(InventoryLevel.on_hand).where(
            InventoryLevel.product_id == po.product_id, InventoryLevel.warehouse_id == po.warehouse_id))
        receive_purchase_order(s, po.id, actual_days=3)
    with session_scope() as s:
        assert s.scalar(select(InventoryLevel.on_hand).where(
            InventoryLevel.product_id == po.product_id,
            InventoryLevel.warehouse_id == po.warehouse_id)) == wh_before + po.quantity
        assert auto_resolve(s) == 1
    d = disruption(run["disruption_id"])
    assert d.status == "resolved" and d.postmortem["summary"]


def test_mitigation_supplier_score_updates_on_late_delivery():
    run = trigger("shipment_delay")["runs"][0]
    d = disruption(run["disruption_id"])
    alt = next(o for o in d.options if o["kind"] == "purchase")
    resume(d.thread_id, {"approved": True, "option_id": alt["id"]})
    with session_scope() as s:
        before = s.get(Supplier, alt["supplier_id"]).reliability_score
        po = s.scalar(select(PurchaseOrder).where(PurchaseOrder.disruption_id == d.id))
        receive_purchase_order(s, po.id, actual_days=po.expected_days * 2)  # delivered twice as slow as promised
        auto_resolve(s)
    with session_scope() as s:
        assert s.get(Supplier, alt["supplier_id"]).reliability_score < before
        assert s.scalars(select(SupplierScoreHistory)).all()


def test_ingestion_rejects_unknown_references():
    with pytest.raises(IngestError), session_scope() as s:
        apply_event(s, make_event("inventory.update", {"sku": "NOPE", "warehouse": "DAL", "on_hand": 1}))
