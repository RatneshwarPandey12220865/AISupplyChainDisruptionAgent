"""Monte Carlo Simulation node: turns the impact projection into a loss distribution for "do nothing"."""
from sqlalchemy.orm import Session

from app.agents.common import delay_stats, record
from app.agents.state import AgentState
from app.config import settings
from app.db import session_scope
from app.simulation import Inbound, MonteCarloSimulator, ScenarioInput


def build_scenario(session: Session, state: AgentState) -> tuple[ScenarioInput, list[Inbound]]:
    a, imp = state["anomaly"], state["impact"]
    inbounds = [Inbound(label=f"inbound:{i['code']}", qty=i["qty"], base_day=i["day"], delay_std=0.75)
                for i in imp["other_inbound"]]
    if imp["has_baseline_inbound"]:
        hist_mean, hist_std = (delay_stats(session, a["supplier_id"], a["product_id"])
                               if a["supplier_id"] else (0.0, 1.5))
        risk = a.get("regional_risk", 0.0)
        delay_mean = max(a["delay_days"] * (1 + 0.5 * risk) + max(hist_mean, 0), 0.0)
        inbounds.append(Inbound(label="affected", qty=a["affected_qty"], base_day=a["base_arrival_days"],
                                delay_mean=delay_mean, delay_std=max(hist_std, 0.35 * delay_mean)))
    scenario = ScenarioInput(
        on_hand=imp["on_hand"], demand_mean=imp["daily_demand"], demand_std=imp["daily_demand_std"],
        loss_per_unit=imp["loss_per_unit"], daily_penalty=imp["daily_vip_penalty"],
        horizon_days=imp["horizon_days"], runs=settings.monte_carlo_runs,
        seed=state["disruption_id"] or 7)
    return scenario, inbounds


def simulation_node(state: AgentState) -> dict:
    with session_scope() as s:
        scenario, inbounds = build_scenario(s, state)
        result = MonteCarloSimulator(scenario).run(inbounds)
        result["runs"] = scenario.runs
        result["horizon_days"] = scenario.horizon_days
        record(s, state["disruption_id"], "simulation",
               f"{scenario.runs:,} runs: expected loss ${result['expected_loss']:,.0f}, "
               f"P85 ${result['p85']:,.0f}, P(loss)={result['prob_any_loss']:.0%}",
               simulation=result, value_at_risk=result["expected_loss"],
               expected_recovery_days=result["expected_recovery_day"])
    return {"simulation": result}
