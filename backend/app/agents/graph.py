"""LangGraph multi-agent state graph with a durable checkpointer (Postgres or SQLite).

    detection ─┬─> impact -> simulation -> sourcing -> guardrail ─┬─> execution ──────────────> END
               └─> END (duplicate)                               ├─> monitor ────────────────> END
                                                                 └─> escalate -> human_review ┬─> execution
                                                                          (interrupt)         └─> reject -> END
"""
import logging
import sqlite3
import threading
import uuid

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agents.detection import detection_node
from app.agents.execution import execution_node
from app.agents.guardrails import (escalate_node, guardrail_node, human_review_node, monitor_node, reject_node,
                                   route_after_guardrail, route_after_review)
from app.agents.impact import impact_node
from app.agents.risk_simulation import simulation_node
from app.agents.sourcing import sourcing_node
from app.agents.state import AgentState
from app.config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_graph = None


def _checkpointer():
    if settings.is_postgres:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        conninfo = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
        pool = ConnectionPool(conninfo, max_size=10, open=True,
                              kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(pool)
    else:
        from langgraph.checkpoint.sqlite import SqliteSaver

        saver = SqliteSaver(sqlite3.connect(settings.checkpoint_sqlite_path, check_same_thread=False))
    saver.setup()
    return saver


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    g.add_node("detection", detection_node)
    g.add_node("impact", impact_node)
    g.add_node("simulation", simulation_node)
    g.add_node("sourcing", sourcing_node)
    g.add_node("guardrail", guardrail_node)
    g.add_node("escalate", escalate_node)
    g.add_node("human_review", human_review_node)
    g.add_node("execution", execution_node)
    g.add_node("reject", reject_node)
    g.add_node("monitor", monitor_node)

    g.add_edge(START, "detection")
    g.add_conditional_edges("detection", lambda s: END if s.get("duplicate_of") else "impact", ["impact", END])
    g.add_edge("impact", "simulation")
    g.add_edge("simulation", "sourcing")
    g.add_edge("sourcing", "guardrail")
    g.add_conditional_edges("guardrail", route_after_guardrail, ["execution", "escalate", "monitor"])
    g.add_edge("escalate", "human_review")
    g.add_conditional_edges("human_review", route_after_review, ["execution", "reject"])
    for node in ("execution", "reject", "monitor"):
        g.add_edge(node, END)
    return g.compile(checkpointer=checkpointer or _checkpointer())


def get_graph():
    global _graph
    with _lock:
        if _graph is None:
            _graph = build_graph()
    return _graph


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _summary(thread_id: str, state: dict) -> dict:
    snapshot = get_graph().get_state(_config(thread_id))
    return {"thread_id": thread_id, "disruption_id": state.get("disruption_id"),
            "duplicate_of": state.get("duplicate_of"), "status": state.get("status"),
            "interrupted": bool(snapshot.next)}


def run_anomaly(anomaly: dict, event: dict) -> dict:
    thread_id = uuid.uuid4().hex
    state = get_graph().invoke({"event": event, "anomaly": anomaly}, _config(thread_id))
    return _summary(thread_id, state)


def resume(thread_id: str, decision: dict) -> dict:
    state = get_graph().invoke(Command(resume=decision), _config(thread_id))
    return _summary(thread_id, state)


def is_waiting(thread_id: str) -> bool:
    return "human_review" in (get_graph().get_state(_config(thread_id)).next or ())
