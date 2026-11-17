"""Agent worker: consumes the event bus, runs detection + the LangGraph pipeline, and runs background jobs
(post-mortems, signal expiry, ERP outbox retries, external data + carrier tracking polling, MQTT bridge).

Runs embedded inside the API process by default; with Redis configured you can scale out by running
`python -m app.worker` processes (set RUN_EMBEDDED_WORKER=false on the API).

Delivery is at-least-once, so `process_event` first *claims* the event in `event_log` (queued -> processing)
with a compare-and-set update; a redelivered or concurrently-delivered event is skipped. A claim that is older
than EVENT_CLAIM_TIMEOUT_SECONDS is treated as a crashed worker and taken over.
"""
import asyncio
import logging
import socket
import time
from datetime import timedelta

from sqlalchemy import select, update

from app.agents.detection import detect_anomalies
from app.agents.graph import run_anomaly
from app.config import settings
from app.db import init_db, session_scope
from app.events import get_bus, make_event
from app.metrics import AGENT_RUN_SECONDS, DISRUPTIONS, EVENTS
from app.models import EventLog, ExternalSignal, utcnow
from app.services import erp, external_data, feeds
from app.services.ingestion import apply_event
from app.services.postmortem import auto_resolve

log = logging.getLogger(__name__)


def claim_event(event_id: str) -> bool:
    """Atomically take ownership of an event. False if it is already processed/being processed elsewhere."""
    with session_scope() as s:
        row = s.scalar(select(EventLog).where(EventLog.event_id == event_id))
        if row is None:
            return True  # not applied through ingest (e.g. direct publish): nothing to dedupe against
        now = utcnow()
        stale = row.claimed_at is not None and now - row.claimed_at > timedelta(seconds=settings.event_claim_timeout_seconds)
        if row.status in ("processed", "failed") or (row.status == "processing" and not stale):
            return False
        unchanged = EventLog.claimed_at.is_(None) if row.claimed_at is None else EventLog.claimed_at == row.claimed_at
        won = s.execute(update(EventLog).where(EventLog.id == row.id, EventLog.status == row.status, unchanged)
                        .values(status="processing", claimed_at=now)).rowcount
        return won == 1


def process_event(event: dict) -> dict:
    if not claim_event(event["event_id"]):
        log.info("Skipping duplicate/in-flight event %s", event.get("event_id"))
        EVENTS.labels(event.get("type", "?"), "duplicate").inc()
        return {"skipped": "duplicate"}
    started = time.monotonic()
    try:
        with session_scope() as s:
            anomalies = detect_anomalies(s, event)
        runs = [run_anomaly(a, event) for a in anomalies]
        for r in runs:
            DISRUPTIONS.labels(r.get("status") or "unknown").inc()
        result, status = {"anomalies": len(anomalies), "runs": runs}, "processed"
    except Exception as exc:
        log.exception("Processing failed for event %s", event.get("event_id"))
        result, status = {"error": str(exc)}, "failed"
    AGENT_RUN_SECONDS.observe(time.monotonic() - started)
    EVENTS.labels(event.get("type", "?"), status).inc()
    with session_scope() as s:
        row = s.scalar(select(EventLog).where(EventLog.event_id == event["event_id"]))
        if row:
            row.status, row.result = status, result
    return result


async def handle(event: dict) -> None:
    await asyncio.to_thread(process_event, event)


async def ingest(event: dict) -> dict:
    """Apply an event to the system of record, then publish it for the agents."""
    def _apply():
        with session_scope() as s:
            return apply_event(s, event)
    event = await asyncio.to_thread(_apply)
    await (await get_bus()).publish(event)
    return event


async def ingest_payload(type_: str, payload: dict, source: str) -> None:
    try:
        await ingest(make_event(type_, payload, source=source))
    except Exception as exc:  # e.g. telemetry for an unknown shipment: log, don't kill the feed
        log.warning("Dropped %s from %s: %s", type_, source, exc)


def _housekeeping() -> None:
    with session_scope() as s:
        s.execute(update(ExternalSignal).where(ExternalSignal.active, ExternalSignal.expires_at < utcnow())
                  .values(active=False))
        resolved = auto_resolve(s)
        if resolved:
            log.info("Auto-resolved %d disruption(s)", resolved)
    if settings.erp_webhook_url:
        with session_scope() as s:
            stats = erp.retry_pending(s)
        if stats["synced"] or stats["failed"]:
            log.info("ERP outbox sweep: %s", stats)


def _poll_external() -> list[dict]:
    with session_scope() as s:
        return external_data.poll_weather(s) + external_data.poll_news(s)


def _poll_tracking() -> list[dict]:
    with session_scope() as s:
        return feeds.poll_tracking(s)


async def background_loop() -> None:
    last_poll = last_track = 0.0
    while True:
        try:
            await asyncio.to_thread(_housekeeping)
            now = time.time()
            if settings.enable_external_polling and now - last_poll > settings.external_poll_interval_seconds:
                last_poll = now
                for sig in await asyncio.to_thread(_poll_external):
                    await ingest(make_event("external.signal", sig, source=sig.get("source", "poller")))
            if settings.enable_tracking_polling and now - last_track > settings.tracking_poll_interval_seconds:
                last_track = now
                for upd in await asyncio.to_thread(_poll_tracking):
                    source = upd.pop("source", "tracking")
                    await ingest_payload("shipment.eta_update", upd, source)
        except Exception:
            log.exception("Background job failed")
        await asyncio.sleep(settings.background_interval_seconds)


async def run_worker() -> None:
    bus = await get_bus()
    consumer = socket.gethostname()  # stable name so un-acked entries are re-claimed after a restart
    log.info("Agent worker started (bus=%s, consumer=%s, llm=%s)", bus.name, consumer,
             settings.deepseek_model if settings.llm_enabled else "disabled/fallback")
    bridge = feeds.MqttBridge(asyncio.get_running_loop(), lambda t, p, src: ingest_payload(t, p, src))
    bridge.start()
    try:
        await asyncio.gather(bus.consume(handle, consumer), background_loop())
    finally:
        bridge.stop()


if __name__ == "__main__":
    from app.logging_setup import setup_logging

    setup_logging()
    for warning in settings.validate_for_runtime():
        log.warning(warning)
    if settings.worker_metrics_port:
        from prometheus_client import start_http_server

        start_http_server(settings.worker_metrics_port)
        log.info("Worker metrics on :%d/metrics", settings.worker_metrics_port)
    init_db()
    asyncio.run(run_worker())
