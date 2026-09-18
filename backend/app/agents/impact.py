"""2. Impact Analysis agent: queries inventory, inbound shipments and open orders to project the delay
cascade (stockout window, orders at risk, VIP exposure, $ loss). Numbers are computed deterministically;
DeepSeek only writes the planner-facing narrative."""
import math

from pydantic import BaseModel
from sqlalchemy import select

from app.agents.common import days_until, record
from app.agents.state import AgentState
from app.config import settings
from app.db import session_scope
from app.llm import structured_call
from app.models import CustomerOrder, InventoryLevel, Product, Shipment

REPLENISH_CYCLE_DAYS = 14  # coverage a normal reorder buys (low-stock anomalies)


class ImpactNarrative(BaseModel):
    narrative: str
    key_risks: list[str]


def _project(on_hand: float, demand: float, inbound: list[tuple[float, float]], horizon: int) -> dict:
    """Expected-value daily projection. inbound = [(day, qty)]."""
    inv, lost = on_hand, 0.0
    start = end = None
    for d in range(horizon):
        inv += sum(q for day, q in inbound if math.floor(day) == d)
        if inv < demand:
            lost += demand - inv
            start = d if start is None else start
            end = d
        inv = max(0.0, inv - demand)
    return {"lost_units": round(lost), "stockout_start_day": start,
            "stockout_end_day": None if end is None else end + 1}


def impact_node(state: AgentState) -> dict:
    a, did = state["anomaly"], state["disruption_id"]
    # Loss is only attributed to this disruption until normal replenishment would have resumed: a few days
    # past the (risk-adjusted, padded) recovery arrival, or one replenishment cycle for low stock.
    if a["type"] == "low_stock":
        window = a["base_arrival_days"] + REPLENISH_CYCLE_DAYS
    else:
        window = a["base_arrival_days"] + 1.5 * a["delay_days"] * (1 + 0.5 * a.get("regional_risk", 0)) + 10
    horizon = int(min(settings.simulation_horizon_days, math.ceil(window)))
    with session_scope() as s:
        product = s.get(Product, a["product_id"])
        on_hand = float(s.scalar(select(InventoryLevel.on_hand).where(
            InventoryLevel.product_id == product.id, InventoryLevel.warehouse_id == a["warehouse_id"])) or 0)
        demand = product.daily_demand_mean

        other = s.scalars(select(Shipment).where(
            Shipment.product_id == product.id, Shipment.warehouse_id == a["warehouse_id"],
            Shipment.status.in_(("in_transit", "delayed")), Shipment.id != (a.get("shipment_id") or -1))).all()
        other_inbound = [{"code": sh.code, "qty": sh.quantity, "day": round(days_until(sh.eta), 2)} for sh in other]

        has_baseline = a["type"] != "low_stock"  # low stock: nothing is on order until we act
        arrival_day = a["base_arrival_days"] + a["delay_days"]
        inbound = [(i["day"], i["qty"]) for i in other_inbound]
        if has_baseline:
            inbound.append((arrival_day, a["affected_qty"]))
        proj = _project(on_hand, demand, inbound, horizon)

        orders_at_risk, revenue_at_risk, penalty_exposure = [], 0.0, 0.0
        if proj["stockout_start_day"] is not None:
            open_orders = s.scalars(select(CustomerOrder).where(
                CustomerOrder.product_id == product.id, CustomerOrder.warehouse_id == a["warehouse_id"],
                CustomerOrder.status == "open").order_by(CustomerOrder.due_date)).all()
            for o in open_orders:
                due = days_until(o.due_date)
                if proj["stockout_start_day"] <= due < proj["stockout_end_day"]:
                    days_late = proj["stockout_end_day"] - due
                    revenue_at_risk += o.quantity * o.unit_price
                    penalty_exposure += o.penalty_per_day_late * days_late
                    orders_at_risk.append({"order_number": o.order_number, "customer": o.customer,
                                           "is_vip": o.is_vip, "quantity": o.quantity, "due_in_days": round(due, 1),
                                           "days_late": round(days_late, 1),
                                           "penalty_per_day_late": o.penalty_per_day_late})

        loss_per_unit = product.unit_price - product.unit_cost + product.stockout_penalty_per_unit
        impact = {
            "on_hand": on_hand, "daily_demand": demand, "daily_demand_std": product.daily_demand_std,
            "days_of_cover": round(on_hand / demand, 2), "has_baseline_inbound": has_baseline,
            "expected_arrival_day": round(arrival_day, 2) if has_baseline else None,
            "other_inbound": other_inbound, "projection": proj, "horizon_days": horizon,
            "shortfall_units": proj["lost_units"], "orders_at_risk": orders_at_risk,
            "vip_affected": any(o["is_vip"] for o in orders_at_risk),
            "revenue_at_risk": round(revenue_at_risk, 2), "penalty_exposure": round(penalty_exposure, 2),
            "daily_vip_penalty": round(sum(o["penalty_per_day_late"] for o in orders_at_risk), 2),
            "loss_per_unit": round(loss_per_unit, 4),
            "deterministic_loss": round(proj["lost_units"] * loss_per_unit + penalty_exposure, 2),
            "replenish_cycle_days": REPLENISH_CYCLE_DAYS,
            "unit_cost": product.unit_cost, "safety_stock": product.safety_stock,
        }

        def fallback() -> ImpactNarrative:
            if proj["stockout_start_day"] is None:
                text = f"{product.sku} stays in stock: {impact['days_of_cover']} days of cover outlast the disruption."
            else:
                text = (f"{product.sku} stocks out in ~{proj['stockout_start_day']} days and recovers around day "
                        f"{proj['stockout_end_day']}, losing ~{proj['lost_units']:,} units; "
                        f"{len(orders_at_risk)} orders (${revenue_at_risk:,.0f}) at risk.")
            risks = [f"VIP order {o['order_number']} ({o['customer']}) late {o['days_late']}d"
                     for o in orders_at_risk if o["is_vip"]]
            return ImpactNarrative(narrative=text, key_risks=risks)

        narrative, source = structured_call(
            "You are the Impact Analysis agent. Explain the computed delay cascade to a supply planner in 2-3 "
            "sentences and list the key business risks. Use ONLY the numbers provided; do not invent figures.",
            {"anomaly": a["description"], "product": product.sku, "impact": {k: v for k, v in impact.items()
                                                                              if k != "other_inbound"}},
            ImpactNarrative, fallback)
        impact.update(narrative.model_dump(), narrative_source=source)
        record(s, did, "impact", f"Projected {proj['lost_units']:,} lost units, "
                                 f"{len(orders_at_risk)} orders at risk", impact=impact,
               value_at_risk=impact["deterministic_loss"])
    return {"impact": impact}
