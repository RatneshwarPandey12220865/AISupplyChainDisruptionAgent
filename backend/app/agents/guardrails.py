"""Dynamic Human-in-the-Loop guardrails.

Plans auto-execute only when they are cheap (< AUTO_APPROVE_LIMIT), don't touch VIP orders, aren't
critical, use a sufficiently reliable supplier and the LLM is confident. Otherwise the graph notifies
planners (Slack/email/dashboard) and halts with `interrupt()` until a decision is posted to the API;
the checkpointer keeps the paused state durable across restarts.
"""
from langgraph.types import interrupt

from app.agents.common import record
from app.agents.state import AgentState
from app.config import settings
from app.db import session_scope
from app.services.notifications import notify


def guardrail_node(state: AgentState) -> dict:
    rec, imp = state["recommendation"], state["impact"]
    opt = rec["option"]
    reasons: list[str] = []
    if opt["kind"] == "wait":
        route = "monitor"
    else:
        if opt["total_cost"] > settings.auto_approve_limit:
            reasons.append(f"Plan cost ${opt['total_cost']:,.0f} exceeds auto-approve limit "
                           f"${settings.auto_approve_limit:,.0f}")
        if imp.get("vip_affected"):
            reasons.append("VIP customer orders affected")
        if state["triage"]["severity"] == "critical":
            reasons.append("Critical severity disruption")
        if opt.get("reliability") is not None and opt["reliability"] < settings.min_auto_supplier_reliability:
            reasons.append(f"Supplier reliability {opt['reliability']:.2f} below "
                           f"{settings.min_auto_supplier_reliability:.2f}")
        if rec.get("confidence", 1) < settings.min_auto_llm_confidence:
            reasons.append(f"Agent confidence {rec['confidence']:.2f} is low")
        route = "escalate" if reasons else "auto_execute"
    guardrail = {"route": route, "reasons": reasons, "limit": settings.auto_approve_limit}
    with session_scope() as s:
        record(s, state["disruption_id"], "guardrail",
               {"auto_execute": "Within guardrails: auto-executing", "monitor": "No action needed: monitoring",
                "escalate": "Escalated for approval: " + "; ".join(reasons)}[route], guardrail=guardrail)
    return {"guardrail": guardrail}


def route_after_guardrail(state: AgentState) -> str:
    return {"auto_execute": "execution", "escalate": "escalate", "monitor": "monitor"}[state["guardrail"]["route"]]


def escalate_node(state: AgentState) -> dict:
    rec, did = state["recommendation"], state["disruption_id"]
    opt = rec["option"]
    urgency = (rec.get("urgency") or {}).get("headline", "")
    with session_scope() as s:
        d = record(s, did, "escalate", "Planner notified; awaiting approval", status="awaiting_approval")
        notify(s, f"Approval needed: {d.title}",
               f"Recommended: {opt['label']} (${opt['total_cost']:,.0f}).\n"
               f"Why escalated: {'; '.join(state['guardrail']['reasons'])}.\n{urgency}\n"
               f"Review: /disruptions/{did}",
               level="critical" if state["triage"]["severity"] in ("high", "critical") else "warning",
               disruption_id=did)
    return {"status": "awaiting_approval"}


def human_review_node(state: AgentState) -> dict:
    valid = {o["id"] for o in state["options"] if o["kind"] != "wait"}
    answer = interrupt({
        "disruption_id": state["disruption_id"],
        "recommended_option_id": state["recommendation"]["option"]["id"],
        "options": sorted(valid),
        "reasons": state["guardrail"]["reasons"],
    })
    # Runs once more on resume, with `answer` = the value passed via Command(resume=...).
    approved = bool(answer.get("approved"))
    option_id = answer.get("option_id") or state["recommendation"]["option"]["id"]
    if approved and option_id not in valid:
        raise ValueError(f"Unknown option {option_id!r}")
    decision = {"approved": approved, "option_id": option_id, "reviewer": answer.get("reviewer", "planner"),
                "comment": answer.get("comment", "")}
    with session_scope() as s:
        record(s, state["disruption_id"], "human_review",
               f"{'Approved' if approved else 'Rejected'} by {decision['reviewer']}"
               + (f" (option {option_id})" if approved else ""), decision=decision)
    return {"decision": decision}


def route_after_review(state: AgentState) -> str:
    return "execution" if state["decision"]["approved"] else "reject"


def reject_node(state: AgentState) -> dict:
    with session_scope() as s:
        record(s, state["disruption_id"], "reject", "Plan rejected; no ERP changes made", status="rejected")
    return {"status": "rejected"}


def monitor_node(state: AgentState) -> dict:
    with session_scope() as s:
        d = record(s, state["disruption_id"], "monitor", "Risk within tolerance; monitoring", status="monitoring")
        notify(s, f"Monitoring: {d.title}", state["recommendation"]["rationale"], level="info", disruption_id=d.id)
    return {"status": "monitoring"}
