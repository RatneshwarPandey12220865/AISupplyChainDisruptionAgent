"""Authentication, RBAC, webhook signatures, rate limiting, idempotency and production start-up validation."""
import json
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import Settings, settings
from app.db import session_scope
from app.models import EventLog, utcnow
from app.security import limiter, sign_body
from app.worker import claim_event, process_event
from tests.conftest import auth

SHIP = {"shipment_code": "SHP-1002", "delay_days": 10}
SHIP_URL = "/api/ingest/shipment/eta"
JSON = {"Content-Type": "application/json"}


def test_health_is_public_and_minimal(client, secured):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_api_requires_key_when_enabled(client, secured):
    assert client.get("/api/disruptions").status_code == 401
    assert client.get("/api/disruptions", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/disruptions", headers=auth("viewer")).status_code == 200
    bearer = {"Authorization": "Bearer " + auth("viewer")["X-API-Key"]}
    me = client.get("/api/me", headers=bearer).json()
    assert me["name"] == "viewer-user" and me["role"] == "viewer" and me["auth_enabled"] is True


def test_open_when_auth_not_configured(client):
    assert client.get("/api/disruptions").status_code == 200  # dev convenience


def test_roles_are_enforced(client, secured, bus):
    assert client.post("/api/simulate/low_stock", headers=auth("viewer")).status_code == 403
    assert client.post("/api/simulate/low_stock", headers=auth("approver")).status_code == 200
    assert client.post("/api/admin/reset", headers=auth("approver")).status_code == 403
    assert client.post("/api/admin/reset", headers=auth("admin")).status_code == 200


def test_admin_reset_can_be_disabled(client, secured, monkeypatch):
    monkeypatch.setattr(settings, "allow_admin_reset", False)
    assert client.post("/api/admin/reset", headers=auth("admin")).status_code == 403


def test_reviewer_identity_comes_from_the_key_not_the_body(client, secured, bus):
    client.post(SHIP_URL, json=SHIP, headers=auth("approver"))
    process_event(bus.events[-1])
    did = client.get("/api/disruptions", headers=auth("viewer")).json()[0]["id"]
    assert client.post(f"/api/disruptions/{did}/decision", json={"approved": True},
                       headers=auth("viewer")).status_code == 403
    r = client.post(f"/api/disruptions/{did}/decision", json={"approved": True, "reviewer": "mallory"},
                    headers=auth("approver"))
    assert r.status_code == 200
    decision = client.get(f"/api/disruptions/{did}", headers=auth("viewer")).json()["decision"]
    assert decision["reviewer"] == "approver-user" and decision["reviewer_role"] == "approver"
    assert decision["decided_at"]


def test_webhook_signature_variants(client, secured, bus):
    body = json.dumps(SHIP).encode()
    assert client.post(SHIP_URL, content=body, headers=JSON).status_code == 401
    assert client.post(SHIP_URL, content=body, headers={**JSON, "X-Webhook-Secret": "nope"}).status_code == 401
    assert client.post(SHIP_URL, content=body, headers={**JSON, "X-Webhook-Secret": "whsec_test_secret"}
                       ).status_code == 200
    sig = sign_body("whsec_test_secret", body)
    assert client.post(SHIP_URL, content=body, headers={**JSON, "X-Signature": sig}).status_code == 200
    assert client.post(SHIP_URL, content=body, headers={**JSON, "X-Signature": sign_body("other", body)}
                       ).status_code == 401
    tampered = json.dumps({**SHIP, "delay_days": 99}).encode()  # signature for a different body
    assert client.post(SHIP_URL, content=tampered, headers={**JSON, "X-Signature": sig}).status_code == 401


def test_webhook_timestamp_replay_protection(client, secured, bus):
    body = json.dumps(SHIP).encode()
    fresh = str(time.time())
    ok = {**JSON, "X-Timestamp": fresh, "X-Signature": sign_body("whsec_test_secret", body, fresh)}
    assert client.post(SHIP_URL, content=body, headers=ok).status_code == 200
    old = str(time.time() - 3600)
    stale = {**JSON, "X-Timestamp": old, "X-Signature": sign_body("whsec_test_secret", body, old)}
    assert client.post(SHIP_URL, content=body, headers=stale).status_code == 401


def test_plain_secret_can_be_disabled(client, secured, monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_plain_secret", False)
    r = client.post(SHIP_URL, json=SHIP, headers={"X-Webhook-Secret": "whsec_test_secret"})
    assert r.status_code == 401


def test_ingest_idempotency_key_dedupes_retries(client, bus):
    h = {"Idempotency-Key": "abc-123"}
    first = client.post(SHIP_URL, json=SHIP, headers=h).json()
    again = client.post(SHIP_URL, json=SHIP, headers=h).json()
    assert first["status"] == "queued" and again["status"] == "duplicate"
    assert first["event_id"] == again["event_id"] and len(bus.events) == 1


def test_rate_limiting(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    codes = [client.get("/api/dashboard").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    monkeypatch.setattr(settings, "ingest_rate_limit_per_minute", 2)
    limiter.hits.clear()
    body = {"kind": "strike", "region": "Europe", "severity": 0.2}
    assert [client.post("/api/ingest/external", json=body).status_code for _ in range(3)] == [200, 200, 429]


def test_metrics_endpoint(client, monkeypatch):
    client.get("/api/dashboard")
    text = client.get("/metrics").text
    assert "http_requests_total" in text and "llm_calls_total" in text
    monkeypatch.setattr(settings, "metrics_token", "scrape-me")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).status_code == 200


def test_demo_scenarios_disabled_when_seed_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "seed_demo_data", False)
    assert client.post("/api/simulate/low_stock").status_code == 404


def test_production_refuses_unsafe_config():
    base = dict(environment="production", database_url="postgresql+psycopg://u:p@h/db")
    with pytest.raises(RuntimeError, match="API_KEYS"):
        Settings(**base, webhook_secret="x" * 20).validate_for_runtime()
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        Settings(**base, api_keys="a:" + "k" * 20).validate_for_runtime()
    with pytest.raises(RuntimeError, match="16 characters"):
        Settings(**base, api_keys="a:short", webhook_secret="x" * 20).validate_for_runtime()
    with pytest.raises(RuntimeError, match="CORS"):
        Settings(**base, api_keys="a:" + "k" * 20, webhook_secret="x" * 20, cors_origins="*").validate_for_runtime()
    ok = Settings(**base, api_keys="a:" + "k" * 20, webhook_secret="x" * 20, cors_origins="https://app.example.com")
    assert ok.validate_for_runtime() == []
    assert not ok.seed_enabled and not ok.admin_reset_enabled
    with pytest.raises(ValueError):
        Settings(api_keys="broken").validate_for_runtime()


def test_stream_change_token_moves_when_data_changes(client, bus):
    from app.api.routes import _change_token

    before = _change_token()
    client.post("/api/ingest/external", json={"kind": "strike", "region": "Europe", "severity": 0.4})
    assert _change_token() != before


def test_events_are_not_reprocessed(client, bus):
    client.post(SHIP_URL, json=SHIP)
    event = bus.events[-1]
    assert process_event(event)["anomalies"] >= 1
    assert process_event(event) == {"skipped": "duplicate"}  # redelivery after a lost ack


def test_stale_claim_is_taken_over_but_fresh_claim_is_not(client, bus):
    client.post("/api/ingest/external", json={"kind": "strike", "region": "Europe", "severity": 0.4})
    event_id = bus.events[-1]["event_id"]
    assert claim_event(event_id) is True
    assert claim_event(event_id) is False  # another worker is mid-flight
    with session_scope() as s:
        row = s.scalar(select(EventLog).where(EventLog.event_id == event_id))
        row.claimed_at = utcnow() - timedelta(seconds=settings.event_claim_timeout_seconds + 5)
    assert claim_event(event_id) is True  # presumed crashed worker
