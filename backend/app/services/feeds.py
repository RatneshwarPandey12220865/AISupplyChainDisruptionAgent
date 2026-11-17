"""Live data feeds: carrier tracking pollers and an MQTT bridge for IoT telemetry.

* Air: FlightAware AeroAPI (FLIGHTAWARE_API_KEY). For shipments with mode="air" and a ``tracking_ref`` (flight
  ident, e.g. "UAL123") it reads ``estimated_in`` of the matching flight.
* Ocean: any HTTP vessel-tracking API (VESSEL_TRACKING_URL with a ``{ref}`` placeholder, e.g. an IMO/MMSI lookup,
  plus VESSEL_TRACKING_ETA_FIELD, a dotted JSON path to the ETA). This is deliberately provider-agnostic: point it
  at VesselFinder, MarineTraffic, Datalastic, etc. and set the ETA path.
* IoT: MQTT (MQTT_URL / MQTT_TOPIC). Messages are JSON ``{"shipment_code","metric","value","lat","lon"}``; if
  ``metric`` is omitted the last topic segment is used.

Pollers only *emit* ``shipment.eta_update`` payloads when the ETA moved by more than ETA_TOLERANCE_HOURS; the
worker runs them through the same ingest -> detection -> agent pipeline as webhooks.
NOTE: the provider response shapes follow their public docs but could not be exercised against live accounts
here; verify with your own keys (the unit tests cover parsing with recorded-style payloads).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.metrics import FEED_POLLS
from app.models import Shipment

log = logging.getLogger(__name__)
ETA_TOLERANCE_HOURS = 1.0


def _dig(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)] if part.isdigit() and int(part) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def _parse_eta(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def parse_flightaware(body: dict) -> datetime | None:
    for flight in body.get("flights") or []:
        eta = _parse_eta(flight.get("estimated_in") or flight.get("scheduled_in"))
        if eta:
            return eta
    return None


def parse_vessel(body: dict) -> datetime | None:
    return _parse_eta(_dig(body, settings.vessel_tracking_eta_field))


def _fetch_air(ref: str) -> datetime | None:
    resp = httpx.get(f"https://aeroapi.flightaware.com/aeroapi/flights/{ref}",
                     headers={"x-apikey": settings.flightaware_api_key}, timeout=15)
    resp.raise_for_status()
    return parse_flightaware(resp.json())


def _fetch_ocean(ref: str) -> datetime | None:
    headers = {settings.vessel_tracking_key_header: settings.vessel_tracking_api_key} \
        if settings.vessel_tracking_api_key else {}
    resp = httpx.get(settings.vessel_tracking_url.format(ref=ref), headers=headers, timeout=15)
    resp.raise_for_status()
    return parse_vessel(resp.json())


def poll_tracking(session: Session, fetch_air=_fetch_air, fetch_ocean=_fetch_ocean) -> list[dict]:
    """Return shipment.eta_update payloads for shipments whose tracked ETA moved."""
    out: list[dict] = []
    shipments = session.scalars(select(Shipment).where(
        Shipment.status.in_(("in_transit", "delayed")), Shipment.tracking_ref != "")).all()
    for sh in shipments:
        try:
            if sh.mode == "air" and settings.flightaware_api_key:
                feed, eta = "flightaware", fetch_air(sh.tracking_ref)
            elif sh.mode != "air" and settings.vessel_tracking_url:
                feed, eta = "vessel", fetch_ocean(sh.tracking_ref)
            else:
                continue
        except Exception as exc:
            log.warning("Tracking poll failed for %s: %s", sh.code, exc)
            FEED_POLLS.labels(sh.mode, "error").inc()
            continue
        if eta is None:
            FEED_POLLS.labels(feed, "no_eta").inc()
            continue
        FEED_POLLS.labels(feed, "ok").inc()
        if abs((eta - sh.eta).total_seconds()) > ETA_TOLERANCE_HOURS * 3600:
            out.append({"shipment_code": sh.code, "new_eta": eta.isoformat(), "reason": f"{feed} tracking update",
                        "source": feed})
    return out


# ---------------------------------------------------------------------------------------------- MQTT
def parse_mqtt_message(topic: str, payload: bytes) -> dict | None:
    """Turn an MQTT message into a telemetry.reading payload (or None if unusable)."""
    try:
        body = json.loads(payload)
        if not isinstance(body, dict):
            return None
        metric = body.get("metric") or topic.rsplit("/", 1)[-1]
        out = {"shipment_code": str(body.get("shipment_code", "")), "metric": str(metric), "value": float(body["value"])}
        for k in ("lat", "lon"):
            if body.get(k) is not None:
                out[k] = float(body[k])
        return out
    except (ValueError, KeyError, TypeError):
        return None


class MqttBridge:
    """Subscribes to MQTT in a background thread and forwards readings into the event pipeline."""

    def __init__(self, loop: asyncio.AbstractEventLoop, forward) -> None:
        self.loop, self.forward, self.client = loop, forward, None

    def start(self) -> bool:
        if not settings.mqtt_url:
            return False
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.warning("MQTT_URL set but paho-mqtt is not installed")
            return False
        url = urlparse(settings.mqtt_url)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if settings.mqtt_username:
            client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        if url.scheme in ("mqtts", "ssl"):
            client.tls_set()

        def on_connect(c, userdata, flags, reason_code, properties=None):
            c.subscribe(settings.mqtt_topic)
            log.info("MQTT connected, subscribed to %s", settings.mqtt_topic)

        def on_message(c, userdata, msg):
            payload = parse_mqtt_message(msg.topic, msg.payload)
            if payload is None:
                FEED_POLLS.labels("mqtt", "bad_message").inc()
                return
            FEED_POLLS.labels("mqtt", "ok").inc()
            asyncio.run_coroutine_threadsafe(self.forward("telemetry.reading", payload, "mqtt"), self.loop)

        client.on_connect, client.on_message = on_connect, on_message
        client.connect_async(url.hostname or "localhost", url.port or (8883 if url.scheme == "mqtts" else 1883))
        client.loop_start()
        self.client = client
        return True

    def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
