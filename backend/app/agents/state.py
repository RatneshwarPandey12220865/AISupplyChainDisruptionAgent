"""LangGraph state shared by all agent nodes. Values are plain JSON so checkpoints stay portable."""
from typing import Any, Optional, TypedDict


class AgentState(TypedDict, total=False):
    event: dict                 # raw bus event that triggered the run
    anomaly: dict               # rule-engine anomaly this run is responsible for
    disruption_id: Optional[int]
    duplicate_of: Optional[int]
    triage: dict                # Detection agent (LLM) classification
    impact: dict                # Impact agent: cover, affected orders, $ exposure
    simulation: dict            # Monte Carlo baseline ("do nothing") risk
    options: list[dict]         # Sourcing agent: evaluated mitigation options
    recommendation: dict        # Sourcing agent (LLM) choice + rationale
    guardrail: dict             # HITL routing decision and reasons
    decision: dict              # human decision (approve/reject/override option)
    execution: dict             # ERP write-back results
    status: str
    errors: list[Any]
