"""Data ingestion layer: telemetry/IoT, supplier portals, ERP. Each call updates the system of record and
publishes an event to the bus; agents process it asynchronously."""
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.api import schemas
from app.db import session_scope
from app.events import make_event
from app.models import EventLog
from app.security import ingest_rate_limit, verify_webhook
from app.services.ingestion import IngestError
from app.worker import ingest

# Rate limit first (cheap, protects against floods and signature brute-forcing), then verify the signature.
router = APIRouter(prefix="/api/ingest", tags=["ingestion"],
                   dependencies=[Depends(ingest_rate_limit), Depends(verify_webhook)])


def _already_seen(event_id: str) -> bool:
    with session_scope() as s:
        return s.scalar(select(EventLog.id).where(EventLog.event_id == event_id)) is not None


async def _publish(request: Request, type_: str, body, source: str) -> dict:
    """Ingest one event. Senders may pass an `Idempotency-Key` header: replays of the same key return the
    original result instead of creating a second event (safe webhook retries)."""
    payload = body.model_dump(mode="json", exclude_none=True)
    event = make_event(type_, payload, source)
    key = request.headers.get("idempotency-key")
    if key:
        event["event_id"] = hashlib.sha256(f"{type_}:{key}".encode()).hexdigest()[:32]
        if _already_seen(event["event_id"]):
            return {"event_id": event["event_id"], "type": type_, "status": "duplicate"}
    try:
        event = await ingest(event)
    except (IngestError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"event_id": event["event_id"], "type": type_, "status": "queued"}


@router.post("/erp/inventory")
async def inventory(request: Request, body: schemas.InventoryUpdate):
    return await _publish(request, "inventory.update", body, "erp")


@router.post("/erp/order")
async def order(request: Request, body: schemas.OrderCreated):
    return await _publish(request, "order.created", body, "erp")


@router.post("/supplier")
async def supplier(request: Request, body: schemas.SupplierUpdate):
    return await _publish(request, "supplier.update", body, "supplier_portal")


@router.post("/shipment/eta")
async def shipment_eta(request: Request, body: schemas.ShipmentEtaUpdate):
    return await _publish(request, "shipment.eta_update", body, "tracking")


@router.post("/telemetry")
async def telemetry(request: Request, body: schemas.TelemetryReading):
    return await _publish(request, "telemetry.reading", body, "iot")


@router.post("/external")
async def external(request: Request, body: schemas.ExternalSignalIn):
    return await _publish(request, "external.signal", body, "external")
