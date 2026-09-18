"""4. Execution agent: writes the chosen plan back to the ERP (POs, expedites, transfers) and alerts."""
from sqlalchemy import select

from app.agents.common import record
from app.agents.state import AgentState
from app.db import session_scope
from app.models import InventoryLevel
from app.services.erp import create_order
from app.services.notifications import notify


def execution_node(state: AgentState) -> dict:
    a, did = state["anomaly"], state["disruption_id"]
    decision = state.get("decision") or {}
    option_id = decision.get("option_id") or state["recommendation"]["option"]["id"]
    opt = next(o for o in state["options"] if o["id"] == option_id)
    approved_by_human = bool(decision.get("approved"))

    with session_scope() as s:
        if opt["kind"] == "purchase":
            po = create_order(s, kind="purchase", supplier_id=opt["supplier_id"], product_id=a["product_id"],
                              warehouse_id=a["warehouse_id"], quantity=opt["quantity"], unit_price=opt["unit_price"],
                              total_cost=opt["total_cost"], expected_days=opt["inbound"]["base_day"],
                              disruption_id=did)
        elif opt["kind"] == "expedite":
            po = create_order(s, kind="expedite", supplier_id=opt["supplier_id"], product_id=a["product_id"],
                              warehouse_id=a["warehouse_id"], quantity=opt["quantity"], unit_price=opt["unit_price"],
                              total_cost=opt["total_cost"],
                              expected_days=a["base_arrival_days"] + a["delay_days"] * 0.5, disruption_id=did)
        elif opt["kind"] == "transfer":
            src = s.scalar(select(InventoryLevel).where(InventoryLevel.product_id == a["product_id"],
                                                        InventoryLevel.warehouse_id == opt["source_warehouse_id"]))
            src.on_hand -= opt["quantity"]  # stock leaves the source now, lands at destination on receipt
            po = create_order(s, kind="transfer", product_id=a["product_id"], warehouse_id=a["warehouse_id"],
                              source_warehouse_id=opt["source_warehouse_id"], quantity=opt["quantity"],
                              unit_price=opt["unit_price"], total_cost=opt["total_cost"],
                              expected_days=opt["lead_time_days"], disruption_id=did)
        else:
            raise ValueError(f"Cannot execute option kind {opt['kind']!r}")

        execution = {"option_id": opt["id"], "kind": opt["kind"], "po_number": po.po_number,
                     "purchase_order_id": po.id, "total_cost": po.total_cost, "erp_ref": po.erp_ref,
                     "mode": "human_approved" if approved_by_human else "auto"}
        status = "approved_executed" if approved_by_human else "auto_executed"
        d = record(s, did, "execution", f"{po.po_number} written to ERP ({execution['mode']})",
                   execution=execution, status=status)
        notify(s, f"Executed: {d.title}", f"{opt['label']} — {po.po_number}, ${po.total_cost:,.0f}",
               level="info", disruption_id=did)
    return {"execution": execution, "status": status}
