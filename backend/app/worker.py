"""Agent worker: consumes the event bus, runs detection + the LangGraph pipeline, and runs background jobs
(post-mortems, signal expiry, external data polling).

Runs embedded inside the API process by default; with Redis configured you can scale out by running
`python -m app.worker` processes (set RUN_EMBEDDED_WORKER=false on the API).
"""
import asyncio
import logging
import socket
import time

from sqlalchemy import select, update

from app.agents.detection import detect_anomalies
from app.agents.graph import run_anomaly
from app.config import settings
from app.db import init_db, session_scope
from app.events import get_bus, make_event
from app.models import EventLog, ExternalSignal, utcnow
from app.services import external_data
from app.services.ingestion import apply_event
from app.services.postmortem import auto_resolve

log = logging.getLogger(__name__)


def process_event(event: dict) -> dict:
    try:
        with session_scope() as s:
            anomalies = detect_anomalies(s, event)
        runs = [run_anomaly(a, event) for a in anomalies]
        result, status = {"anomalies": len(anomalies), "runs": runs}, "processed"
    except Exception as exc:
        log.exception("Processing failed for event %s", event.get("event_id"))
        result, status = {"error": str(exc)}, "failed"
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


def _housekeeping() -> None:
    with session_scope() as s:
        s.execute(update(ExternalSignal).where(ExternalSignal.active, ExternalSignal.expires_at < utcnow())
                  .values(active=False))
        resolved = auto_resolve(s)
        if resolved:
            log.info("Auto-resolved %d disruption(s)", resolved)


def _poll_external() -> list[dict]:
    with session_scope() as s:
        return external_data.poll_weather(s) + external_data.poll_news(s)


async def background_loop() -> None:
    last_poll = 0.0
    while True:
        try:
            await asyncio.to_thread(_housekeeping)
            if settings.enable_external_polling and time.time() - last_poll > settings.external_poll_interval_seconds:
                last_poll = time.time()
                for sig in await asyncio.to_thread(_poll_external):
                    await ingest(make_event("external.signal", sig, source=sig.get("source", "poller")))
        except Exception:
            log.exception("Background job failed")
        await asyncio.sleep(settings.background_interval_seconds)


async def run_worker() -> None:
    bus = await get_bus()
    consumer = socket.gethostname()  # stable name so un-acked entries are re-claimed after a restart
    log.info("Agent worker started (bus=%s, consumer=%s, llm=%s)", bus.name, consumer,
             settings.deepseek_model if settings.llm_enabled else "disabled/fallback")
    await asyncio.gather(bus.consume(handle, consumer), background_loop())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    asyncio.run(run_worker())
