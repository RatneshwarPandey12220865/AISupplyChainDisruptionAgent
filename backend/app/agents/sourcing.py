"""3. Sourcing & Action agent.

Builds candidate mitigations (wait, expedite, alternative-supplier POs, inter-warehouse transfers), prices
them with live quotes, evaluates each with the same Monte Carlo engine (common random numbers), pulls
relevant contract/SLA clauses via RAG, and asks DeepSeek to choose and justify a recovery plan.
"""
import math
import random
import time

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.common import record, regional_risk
from app.agents.risk_simulation import build_scenario
from app.agents.state import AgentState
from app.db import session_scope
from app.llm import structured_call
from app.models import InventoryLevel, Product, Supplier, SupplierProduct, Warehouse
from app.rag import search_contracts
from app.simulation import Inbound, MonteCarloSimulator

ENGAGE_DAYS = 0.25          # ~6h to place and confirm an order
TRANSFER_DAYS = 2.0
ACT_WINDOWS_HOURS = [0, 6, 12, 24, 48, 72]


def live_quote(session: Session, sp: SupplierProduct) -> dict:
    """Current price/lead time. Stands in for a supplier pricing API: list price with a small intraday
    drift plus a surcharge driven by active risk in the supplier's region."""
    drift = random.Random(f"{sp.id}:{int(time.time() // 3600)}").uniform(-0.03, 0.04)
    risk = regional_risk(session, sp.supplier.region)
    return {"unit_price": round(sp.unit_price * (1 + drift + 0.25 * risk), 4),
            "lead_time_days": round(sp.lead_time_days * (1 + 0.5 * risk), 1), "regional_risk": risk}


def _round_up(qty: float, step: int = 50) -> float:
    return float(math.ceil(max(qty, 0) / step) * step)


def build_options(session: Session, state: AgentState) -> list[dict]:
    a, imp = state["anomaly"], state["impact"]
    product = session.get(Product, a["product_id"])
    demand = imp["daily_demand"]
    baseline_arrival = imp["expected_arrival_day"]
    options: list[dict] = [{
        "id": "wait", "kind": "wait", "label": "Monitor only (no action)", "supplier_id": None,
        "quantity": 0, "unit_price": 0, "total_cost": 0, "premium_cost": 0, "lead_time_days": None,
        "reliability": None, "inbound": None,
    }]

    def needed_qty(arrival_day: float) -> float:
        if baseline_arrival is None:  # low stock: buy a normal replenishment cycle
            return demand * imp["replenish_cycle_days"] + imp["safety_stock"]
        return demand * max(0.0, baseline_arrival - arrival_day) + imp["safety_stock"]

    # Expedite the disrupted flow (premium freight / priority production)
    if a["supplier_id"] and a["type"] in ("shipment_delay", "lead_time_increase", "external_risk"):
        sp = session.scalar(select(SupplierProduct).where(SupplierProduct.supplier_id == a["supplier_id"],
                                                          SupplierProduct.product_id == product.id))
        if sp and sp.expedite_available:
            fee = sp.expedite_fee_pct * a["affected_qty"] * sp.unit_price
            options.append({
                "id": f"expedite-{sp.supplier_id}", "kind": "expedite", "supplier_id": sp.supplier_id,
                "label": f"Expedite with {sp.supplier.name} (halve the delay)", "quantity": a["affected_qty"],
                "unit_price": round(sp.expedite_fee_pct * sp.unit_price, 4), "total_cost": round(fee, 2),
                "premium_cost": round(fee, 2), "lead_time_days": None, "reliability": sp.supplier.reliability_score,
                "inbound": {"expedite_factor": 0.5},
            })

    # Alternative (or, for low stock, primary) suppliers
    stmt = select(SupplierProduct).join(Supplier).where(SupplierProduct.product_id == product.id, Supplier.is_active,
                                                        Supplier.status == "operational")
    if a["type"] != "low_stock" and a["supplier_id"]:
        stmt = stmt.where(SupplierProduct.supplier_id != a["supplier_id"])
    for sp in session.scalars(stmt).all():
        quote = live_quote(session, sp)
        arrival = ENGAGE_DAYS + quote["lead_time_days"]
        qty = _round_up(needed_qty(arrival))
        qty = max(qty, sp.min_order_qty)
        qty = min(qty, _round_up(sp.capacity_per_week * max(1.0, quote["lead_time_days"] / 7)))
        if qty <= 0:
            continue
        spend = qty * quote["unit_price"]
        excess_carry = 0.03 * spend if baseline_arrival is not None else 0.0
        rel = sp.supplier.reliability_score
        options.append({
            "id": f"po-{sp.supplier_id}", "kind": "purchase", "supplier_id": sp.supplier_id,
            "label": f"PO {qty:,.0f} units from {sp.supplier.name} ({sp.supplier.country})",
            "quantity": qty, "unit_price": quote["unit_price"], "total_cost": round(spend, 2),
            "premium_cost": round(qty * max(0.0, quote["unit_price"] - product.unit_cost) + excess_carry, 2),
            "lead_time_days": quote["lead_time_days"], "reliability": rel, "regional_risk": quote["regional_risk"],
            "inbound": {"base_day": arrival, "delay_mean": quote["regional_risk"] * quote["lead_time_days"] * 0.3,
                        "delay_std": sp.lead_time_std_days * (1 + 2 * (1 - rel))},
        })

    # Inter-warehouse transfer of surplus stock
    for inv in session.scalars(select(InventoryLevel).where(InventoryLevel.product_id == product.id,
                                                            InventoryLevel.warehouse_id != a["warehouse_id"])).all():
        surplus = inv.on_hand - product.reorder_point
        qty = _round_up(min(surplus, needed_qty(TRANSFER_DAYS)), 10) if surplus > demand * 2 else 0
        qty = min(qty, math.floor(surplus))
        if qty <= 0:
            continue
        per_unit = max(0.05, 0.06 * product.unit_cost)
        options.append({
            "id": f"transfer-{inv.warehouse_id}", "kind": "transfer", "supplier_id": None,
            "source_warehouse_id": inv.warehouse_id,
            "label": f"Transfer {qty:,.0f} units from {session.get(Warehouse, inv.warehouse_id).code}",
            "quantity": qty, "unit_price": round(per_unit, 4), "total_cost": round(qty * per_unit, 2),
            "premium_cost": round(qty * per_unit, 2), "lead_time_days": TRANSFER_DAYS, "reliability": 0.98,
            "inbound": {"base_day": TRANSFER_DAYS, "delay_mean": 0, "delay_std": 0.5},
        })
    return options


def _option_inbounds(base: list[Inbound], option: dict, engage_delay_days: float = 0.0) -> list[Inbound]:
    ib = option.get("inbound")
    if not ib:
        return base
    if "expedite_factor" in ib:
        return [Inbound(i.label, i.qty, i.base_day + engage_delay_days, i.delay_mean * ib["expedite_factor"],
                        i.delay_std * ib["expedite_factor"]) if i.label == "affected" else i for i in base]
    return [*base, Inbound(option["id"], option["quantity"], ib["base_day"] + engage_delay_days,
                           ib["delay_mean"], ib["delay_std"])]


class SourcingDecision(BaseModel):
    recommended_option_id: str
    rationale: str = Field(description="Why this option, referencing expected loss, cost and risk")
    risks: list[str]
    next_steps: list[str]
    contract_considerations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


def sourcing_node(state: AgentState) -> dict:
    a, did = state["anomaly"], state["disruption_id"]
    with session_scope() as s:
        scenario, base_inbounds = build_scenario(s, state)
        sim = MonteCarloSimulator(scenario)
        options = build_options(s, state)
        for opt in options:
            r = sim.run(_option_inbounds(base_inbounds, opt))
            opt.update(expected_loss=r["expected_loss"], p85_loss=r["p85"], prob_any_loss=r["prob_any_loss"],
                       expected_stockout_days=r["expected_stockout_days"],
                       expected_recovery_day=r["expected_recovery_day"],
                       expected_total_cost=round(opt["premium_cost"] + r["expected_loss"], 2))
        options.sort(key=lambda o: (o["expected_total_cost"], o["p85_loss"]))
        best = options[0]

        # Time-to-act: how quickly must the best active option be engaged?
        act = next((o for o in options if o["kind"] != "wait"), None)
        baseline = state["simulation"]
        urgency = None
        if act:
            curve = [{"hours": h, "expected_loss": sim.run(_option_inbounds(base_inbounds, act, h / 24))["expected_loss"]}
                     for h in ACT_WINDOWS_HOURS]
            tolerance = curve[0]["expected_loss"] + 0.1 * max(0.0, baseline["expected_loss"] - curve[0]["expected_loss"])
            window = max((c["hours"] for c in curve if c["expected_loss"] <= tolerance), default=0)
            headline = (f"85% probability of ≥ ${baseline['p15']:,.0f} loss if '{act['label']}' is not engaged "
                        f"within {window} hours" if baseline["p15"] > 0 else
                        f"{baseline['prob_any_loss']:.0%} probability of any loss without action")
            urgency = {"option_id": act["id"], "curve": curve, "act_within_hours": window, "headline": headline}

        supplier_ids = [i for i in {a["supplier_id"], *(o["supplier_id"] for o in options)} if i]
        clauses = search_contracts(s, f"{a['type'].replace('_', ' ')} late delivery penalty expedite force majeure "
                                      f"allocation {a['sku']}", supplier_ids, k=4)
        valid_ids = {o["id"] for o in options}

        def fallback() -> SourcingDecision:
            return SourcingDecision(
                recommended_option_id=best["id"],
                rationale=(f"Lowest expected total cost: ${best['expected_total_cost']:,.0f} "
                           f"(premium ${best['premium_cost']:,.0f} + residual expected loss ${best['expected_loss']:,.0f}) "
                           f"versus ${baseline['expected_loss']:,.0f} expected loss if nothing is done."),
                risks=[f"Supplier reliability {best['reliability']:.2f}"] if best.get("reliability") else [],
                next_steps=["Confirm quantities with supplier", "Update customer ETAs for at-risk orders"],
                contract_considerations=[c["title"] for c in clauses[:2]], confidence=0.7)

        slim = [{k: v for k, v in o.items() if k != "inbound"} for o in options]
        decision, source = structured_call(
            "You are the Sourcing & Action agent. Choose the best recovery plan among the evaluated options. "
            "Minimise expected total cost (premium + residual expected loss) but weigh tail risk (p85), supplier "
            "reliability, VIP exposure and contract clauses. recommended_option_id MUST be one of the option ids.",
            {"disruption": state["triage"], "impact": {k: state["impact"][k] for k in (
                "days_of_cover", "shortfall_units", "vip_affected", "revenue_at_risk", "penalty_exposure", "narrative")},
             "baseline_simulation": {k: baseline[k] for k in ("expected_loss", "p15", "p85", "p95", "prob_any_loss")},
             "options": slim, "urgency": urgency and {k: urgency[k] for k in ("act_within_hours", "headline")},
             "contract_clauses": clauses},
            SourcingDecision, fallback)
        if decision.recommended_option_id not in valid_ids:
            decision, source = fallback(), "fallback"

        chosen = next(o for o in options if o["id"] == decision.recommended_option_id)
        recommendation = {**decision.model_dump(), "source": source, "option": chosen, "urgency": urgency,
                          "clauses": clauses}
        record(s, did, "sourcing", f"{len(options)} options evaluated; recommended '{chosen['label']}' ({source})",
               options=options, recommendation=recommendation)
    return {"options": options, "recommendation": recommendation}
