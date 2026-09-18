"""Data ingestion layer: telemetry/IoT, supplier portals, ERP. Each call updates the system of record and
publishes an event to the bus; agents process it asynchronously."""
from fastapi import APIRouter, HTTPException

from app.api import schemas
from app.events import make_event
from app.services.ingestion import IngestError
from app.worker import ingest

router = APIRouter(prefix="/api/ingest", tags=["ingestion"])


async def _publish(type_: str, body, source: str) -> dict:
    payload = body.model_dump(mode="json", exclude_none=True)
    try:
        event = await ingest(make_event(type_, payload, source))
    except (IngestError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"event_id": event["event_id"], "type": type_, "status": "queued"}


@router.post("/erp/inventory")
async def inventory(body: schemas.InventoryUpdate):
    return await _publish("inventory.update", body, "erp")


@router.post("/erp/order")
async def order(body: schemas.OrderCreated):
    return await _publish("order.created", body, "erp")


@router.post("/supplier")
async def supplier(body: schemas.SupplierUpdate):
    return await _publish("supplier.update", body, "supplier_portal")


@router.post("/shipment/eta")
async def shipment_eta(body: schemas.ShipmentEtaUpdate):
    return await _publish("shipment.eta_update", body, "tracking")


@router.post("/telemetry")
async def telemetry(body: schemas.TelemetryReading):
    return await _publish("telemetry.reading", body, "iot")


@router.post("/external")
async def external(body: schemas.ExternalSignalIn):
    return await _publish("external.signal", body, "external")
