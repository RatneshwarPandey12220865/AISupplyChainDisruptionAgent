"""Dashboard queries, HITL approvals, ERP receipts, post-mortems and demo scenarios."""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph import is_waiting, resume
from app.api import schemas
from app.config import settings
from app.db import get_session, session_scope
from app.events import make_event
from app.metrics import SSE_CLIENTS
from app.security import Principal, api_rate_limit, require
from app.models import (CustomerOrder, Disruption, EventLog, ExternalSignal, InventoryLevel, Notification,
                        Product, PurchaseOrder, Shipment, Supplier, SupplierProduct, SupplierScoreHistory, utcnow)
from app.seed import SCENARIOS, reset_database
from app.services.postmortem import receive_purchase_order, run_postmortem
from app.worker import ingest

log = logging.getLogger(__name__)
# Every route here needs at least a viewer key (auth runs first so the rate limiter can key on the user).
router = APIRouter(prefix="/api", dependencies=[Depends(require("viewer")), Depends(api_rate_limit)])
OPEN = ("analyzing", "awaiting_approval", "auto_executed", "approved_executed", "monitoring")


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def disruption_brief(d: Disruption) -> dict:
    opt = (d.recommendation or {}).get("option") or {}
    return {"id": d.id, "title": d.title, "type": d.type, "severity": d.severity, "status": d.status,
            "product": d.product.sku if d.product else None, "supplier": d.supplier.name if d.supplier else None,
            "warehouse": d.warehouse.code if d.warehouse else None, "value_at_risk": d.value_at_risk,
            "recommended": opt.get("label"), "plan_cost": opt.get("total_cost"),
            "created_at": _iso(d.created_at), "updated_at": _iso(d.updated_at)}


@router.get("/me")
def me(principal: Principal = Depends(require("viewer"))):
    """Who am I? Lets the dashboard validate a pasted API key and decide which buttons to show."""
    return {"name": principal.name, "role": principal.role, "auth_method": principal.kind,
            "auth_enabled": settings.auth_enabled,
            "demo_mode": settings.seed_enabled, "admin_reset": settings.admin_reset_enabled}


@router.get("/status")
def status():
    return {"llm": settings.deepseek_model if settings.llm_enabled else "fallback (no DEEPSEEK_API_KEY)",
            "database": "postgres" if settings.is_postgres else "sqlite",
            "event_bus": "redis" if settings.redis_url else "memory",
            "environment": settings.environment, "auto_approve_limit": settings.auto_approve_limit,
            "erp": settings.erp_flavor if settings.erp_webhook_url else "not configured",
            "quotes": "supplier_api" if settings.supplier_quote_url else "model",
            "embeddings": settings.embedding_provider}


@router.get("/dashboard")
def dashboard(s: Session = Depends(get_session)):
    rows = s.scalars(select(Disruption)).all()
    open_rows = [d for d in rows if d.status in OPEN]
    return {
        "kpis": {
            "open_disruptions": len(open_rows),
            "awaiting_approval": sum(d.status == "awaiting_approval" for d in rows),
            "auto_executed": sum((d.execution or {}).get("mode") == "auto" for d in rows),
            "resolved": sum(d.status == "resolved" for d in rows),
            "value_at_risk": round(sum(d.value_at_risk or 0 for d in open_rows if d.status in
                                       ("analyzing", "awaiting_approval", "monitoring")), 2),
            "mitigation_spend": round(sum((d.execution or {}).get("total_cost", 0) for d in rows), 2),
            "active_signals": s.scalar(select(func.count()).select_from(ExternalSignal)
                                       .where(ExternalSignal.active)) or 0,
        },
        "llm": settings.deepseek_model if settings.llm_enabled else None,
        "scenarios": list(SCENARIOS),
    }


@router.get("/disruptions")
def list_disruptions(status: str | None = None, s: Session = Depends(get_session)):
    stmt = select(Disruption).order_by(Disruption.created_at.desc()).limit(200)
    if status:
        stmt = stmt.where(Disruption.status == status)
    return [disruption_brief(d) for d in s.scalars(stmt).all()]


@router.get("/disruptions/{did}")
def get_disruption(did: int, s: Session = Depends(get_session)):
    d = s.get(Disruption, did)
    if not d:
        raise HTTPException(404, "Disruption not found")
    pos = s.scalars(select(PurchaseOrder).where(PurchaseOrder.disruption_id == did)).all()
    return {**disruption_brief(d), "summary": d.summary, "anomaly": d.anomaly, "triage": d.triage,
            "impact": d.impact, "simulation": d.simulation, "options": d.options,
            "recommendation": d.recommendation, "guardrail": d.guardrail, "decision": d.decision,
            "execution": d.execution, "postmortem": d.postmortem, "trace": d.trace,
            "expected_recovery_days": d.expected_recovery_days, "actual_recovery_days": d.actual_recovery_days,
            "resolved_at": _iso(d.resolved_at), "purchase_orders": [po_dict(p) for p in pos]}


@router.post("/disruptions/{did}/decision")
async def decide(did: int, body: schemas.Decision, principal: Principal = Depends(require("approver"))):
    def _thread():
        with session_scope() as s:
            d = s.get(Disruption, did)
            if not d:
                raise HTTPException(404, "Disruption not found")
            return d.thread_id
    thread_id = await run_in_threadpool(_thread)
    if not await run_in_threadpool(is_waiting, thread_id):
        raise HTTPException(409, "Disruption is not awaiting approval")
    try:
        decision = body.model_dump()
        # Identity comes from the authenticated key, never from the request body (audit-grade attribution).
        decision["reviewer"] = principal.name if settings.auth_enabled else (body.reviewer or principal.name)
        decision["reviewer_role"] = principal.role
        decision["decided_at"] = _iso(utcnow())
        return await run_in_threadpool(resume, thread_id, decision)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/disruptions/{did}/resolve")
def resolve(did: int, body: schemas.Resolve, s: Session = Depends(get_session),
            principal: Principal = Depends(require("approver"))):
    d = s.get(Disruption, did)
    if not d:
        raise HTTPException(404, "Disruption not found")
    if d.status in ("analyzing", "awaiting_approval"):
        raise HTTPException(409, f"Cannot resolve while {d.status}")
    run_postmortem(s, did, body.actual_recovery_days, body.actual_loss)
    return {"id": did, "status": "resolved"}


def po_dict(p: PurchaseOrder) -> dict:
    return {"id": p.id, "po_number": p.po_number, "kind": p.kind, "supplier": p.supplier.name if p.supplier else None,
            "product": p.product.sku, "warehouse": p.warehouse.code if p.warehouse else None,
            "quantity": p.quantity, "unit_price": p.unit_price, "total_cost": p.total_cost,
            "expected_days": p.expected_days, "actual_days": p.actual_days, "status": p.status,
            "disruption_id": p.disruption_id, "erp_ref": p.erp_ref, "erp_status": p.erp_status, "created_at": _iso(p.created_at),
            "received_at": _iso(p.received_at)}


@router.get("/purchase-orders")
def purchase_orders(s: Session = Depends(get_session)):
    return [po_dict(p) for p in s.scalars(select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc())).all()]


@router.post("/purchase-orders/{po_id}/receive")
def receive(po_id: int, body: schemas.Receive, s: Session = Depends(get_session),
            principal: Principal = Depends(require("approver"))):
    try:
        return po_dict(receive_purchase_order(s, po_id, body.actual_days))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/suppliers")
def suppliers(s: Session = Depends(get_session)):
    out = []
    for sup in s.scalars(select(Supplier).order_by(Supplier.reliability_score.desc())).all():
        history = s.scalars(select(SupplierScoreHistory).where(SupplierScoreHistory.supplier_id == sup.id)
                            .order_by(SupplierScoreHistory.created_at.desc()).limit(10)).all()
        catalog = s.scalars(select(SupplierProduct).where(SupplierProduct.supplier_id == sup.id)).all()
        out.append({"id": sup.id, "name": sup.name, "country": sup.country, "region": sup.region, "port": sup.port,
                    "lat": sup.lat, "lon": sup.lon, "reliability_score": sup.reliability_score, "status": sup.status,
                    "catalog": [{"sku": c.product.sku, "unit_price": c.unit_price, "lead_time_days": c.lead_time_days,
                                 "capacity_per_week": c.capacity_per_week} for c in catalog],
                    "score_history": [{"old": h.old_score, "new": h.new_score, "reason": h.reason,
                                       "at": _iso(h.created_at)} for h in history]})
    return out


@router.get("/inventory")
def inventory(s: Session = Depends(get_session)):
    rows = s.scalars(select(InventoryLevel).join(Product).order_by(Product.sku)).all()
    return [{"sku": r.product.sku, "name": r.product.name, "warehouse": r.warehouse.code, "on_hand": r.on_hand,
             "reorder_point": r.product.reorder_point, "days_of_cover": round(r.on_hand / r.product.daily_demand_mean, 1)}
            for r in rows]


@router.get("/shipments")
def shipments(s: Session = Depends(get_session)):
    return [{"code": sh.code, "supplier": sh.supplier.name, "sku": sh.product.sku, "warehouse": sh.warehouse.code,
             "quantity": sh.quantity, "eta": _iso(sh.eta), "original_eta": _iso(sh.original_eta),
             "delay_days": round((sh.eta - sh.original_eta).total_seconds() / 86400, 1), "status": sh.status,
             "mode": sh.mode, "carrier": sh.carrier, "vessel": sh.vessel, "route_region": sh.route_region}
            for sh in s.scalars(select(Shipment).order_by(Shipment.eta)).all()]


@router.get("/orders")
def orders(s: Session = Depends(get_session)):
    return [{"order_number": o.order_number, "customer": o.customer, "is_vip": o.is_vip, "quantity": o.quantity,
             "due_date": _iso(o.due_date), "status": o.status, "penalty_per_day_late": o.penalty_per_day_late}
            for o in s.scalars(select(CustomerOrder).order_by(CustomerOrder.due_date)).all()]


@router.get("/signals")
def signals(s: Session = Depends(get_session)):
    return [{"id": x.id, "kind": x.kind, "region": x.region, "severity": x.severity, "description": x.description,
             "source": x.source, "active": x.active, "expires_at": _iso(x.expires_at)}
            for x in s.scalars(select(ExternalSignal).order_by(ExternalSignal.created_at.desc()).limit(50)).all()]


@router.get("/notifications")
def notifications(s: Session = Depends(get_session)):
    return [{"id": n.id, "level": n.level, "title": n.title, "body": n.body, "channels": n.channels,
             "disruption_id": n.disruption_id, "created_at": _iso(n.created_at)}
            for n in s.scalars(select(Notification).order_by(Notification.created_at.desc()).limit(50)).all()]


@router.get("/events")
def events(s: Session = Depends(get_session)):
    return [{"event_id": e.event_id, "type": e.type, "source": e.source, "payload": e.payload, "status": e.status,
             "result": e.result, "received_at": _iso(e.received_at)}
            for e in s.scalars(select(EventLog).order_by(EventLog.received_at.desc()).limit(50)).all()]


@router.post("/simulate/{scenario}")
async def simulate(scenario: str, principal: Principal = Depends(require("approver"))):
    if not settings.seed_enabled:
        raise HTTPException(404, "Demo scenarios are disabled (SEED_DEMO_DATA is off)")
    if scenario not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario; choose from {list(SCENARIOS)}")
    type_, payload = SCENARIOS[scenario]
    event = await ingest(make_event(type_, dict(payload), source=f"demo:{scenario}"))
    return {"event_id": event["event_id"], "type": type_, "status": "queued", "at": _iso(utcnow())}


@router.post("/admin/reset")
async def reset(principal: Principal = Depends(require("admin"))):
    """Wipes and re-seeds the database. Admin-only and disabled unless ALLOW_ADMIN_RESET (default: dev only)."""
    if not settings.admin_reset_enabled:
        raise HTTPException(403, "Database reset is disabled in this environment")
    log.warning("Database reset requested by %s", principal.name)
    await run_in_threadpool(reset_database)
    return {"status": "reset"}


def _change_token() -> str:
    """Cheap fingerprint of 'something changed' that works across processes (API and workers share the DB)."""
    with session_scope() as s:
        parts = [
            s.execute(select(func.max(Disruption.updated_at), func.count(Disruption.id))).one(),
            s.scalar(select(func.max(EventLog.id))), s.scalar(select(func.max(Notification.id))),
            s.execute(select(func.max(PurchaseOrder.id), func.count(PurchaseOrder.id))
                      .where(PurchaseOrder.status == "received")).one(),
            s.scalar(select(func.max(PurchaseOrder.id))), s.scalar(select(func.max(ExternalSignal.id))),
            s.scalar(select(func.max(SupplierScoreHistory.id))),
        ]
    return json.dumps(parts, default=str)


@router.get("/stream")
async def stream(request: Request):
    """Server-Sent Events: emits `change` whenever the database moved, so the UI refetches instead of blind
    polling. Clients connect with fetch() + an Authorization header (EventSource can't send headers)."""
    async def gen():
        SSE_CLIENTS.inc()
        last, last_beat = None, time.monotonic()
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                token = await run_in_threadpool(_change_token)
                now = time.monotonic()
                if token != last:
                    last, last_beat = token, now
                    yield f"event: change\ndata: {json.dumps({'at': _iso(utcnow())})}\n\n"
                elif now - last_beat > 15:
                    last_beat = now
                    yield ": keepalive\n\n"
                await asyncio.sleep(settings.sse_poll_seconds)
        finally:
            SSE_CLIENTS.dec()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
