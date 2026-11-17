"""ERP outbox/retries, supplier quote API, embeddings provider, LLM budget + metering, live feeds, migrations."""
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

import app.llm as llm
import app.rag as rag
import app.services.erp as erp
import app.services.quotes as quotes
from app.agents.detection import Triage
from app.agents.sourcing import live_quote
from app.config import settings
from app.db import session_scope
from app.models import LLMUsage, PurchaseOrder, Shipment, SupplierProduct, utcnow
from app.services import feeds


# ------------------------------------------------------------------------------------------------- ERP
def _make_po(**kw):
    with session_scope() as s:
        sp = s.scalar(select(SupplierProduct))
        po = erp.create_order(s, kind="purchase", supplier_id=sp.supplier_id, product_id=sp.product_id,
                              warehouse_id=1, quantity=100, unit_price=2.5, total_cost=250, expected_days=7,
                              disruption_id=None, **kw)
        return po.po_number


def _po(number):
    with session_scope() as s:
        return s.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == number))


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
        self.headers = {"content-type": "application/json"}
        self.request = httpx.Request("POST", "http://erp")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=self.request, response=self)


@pytest.fixture
def erp_on(monkeypatch):
    monkeypatch.setattr(settings, "erp_webhook_url", "http://erp.local/po")
    monkeypatch.setattr(settings, "erp_auth_value", "Bearer tkn")
    monkeypatch.setattr(erp.time, "sleep", lambda s: None)


def test_erp_not_configured_marks_po(client):
    assert _po(_make_po()).erp_status == "not_configured"


def test_erp_retries_transient_failures_with_idempotency_key(client, erp_on, monkeypatch):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(headers)
        return FakeResp(503) if len(calls) < 3 else FakeResp(200, {"id": "ERP-77"})

    monkeypatch.setattr(erp.httpx, "post", post)
    po = _po(_make_po())
    assert po.erp_status == "synced" and po.erp_ref == "ERP-77" and po.erp_attempts == 3
    assert len(calls) == 3
    assert all(h["Idempotency-Key"] == po.po_number and h["Authorization"] == "Bearer tkn" for h in calls)


def test_erp_permanent_error_is_not_retried(client, erp_on, monkeypatch):
    calls = []
    monkeypatch.setattr(erp.httpx, "post", lambda *a, **k: calls.append(1) or FakeResp(400))
    po = _po(_make_po())
    assert po.erp_status == "failed" and len(calls) == 1


def test_erp_outage_queues_for_background_retry_then_gives_up(client, erp_on, monkeypatch):
    state = {"up": False}

    def post(*a, **k):
        if not state["up"]:
            raise httpx.ConnectError("down")
        return FakeResp(200, {"id": "ERP-1"})

    monkeypatch.setattr(erp.httpx, "post", post)
    number = _make_po()
    assert _po(number).erp_status == "pending" and _po(number).erp_attempts == settings.erp_max_attempts
    state["up"] = True
    with session_scope() as s:
        assert erp.retry_pending(s, sleep=lambda x: None)["synced"] == 1
    assert _po(number).erp_status == "synced"

    # permanent outage: after the attempt cap the PO is failed and planners are alerted
    state["up"] = False
    monkeypatch.setattr(settings, "erp_retry_max_total_attempts", 5)
    number = _make_po()
    with session_scope() as s:
        assert erp.retry_pending(s, sleep=lambda x: None)["failed"] == 1
    assert _po(number).erp_status == "failed"


@pytest.mark.parametrize("flavor,key", [("generic", "po_number"), ("sap", "to_PurchaseOrderItem"),
                                        ("netsuite", "externalId")])
def test_erp_payload_flavors(client, flavor, key, monkeypatch):
    monkeypatch.setattr(settings, "erp_flavor", flavor)
    number = _make_po()
    with session_scope() as s:
        po = s.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == number))
        assert key in erp.build_payload(po, s)


# --------------------------------------------------------------------------------------------- quotes
def test_live_quote_uses_supplier_api_then_falls_back(client, monkeypatch):
    monkeypatch.setattr(settings, "supplier_quote_url", "http://pricing.local/quote")
    quotes._cache.clear()
    sent = []

    def post(url, headers=None, timeout=None, json=None):
        sent.append(json)
        return FakeResp(200, {"unit_price": 9.99, "lead_time_days": 11})

    monkeypatch.setattr(quotes.httpx, "post", post)
    with session_scope() as s:
        sp = s.scalar(select(SupplierProduct))
        q = live_quote(s, sp)
        assert q["unit_price"] == 9.99 and q["lead_time_days"] == 11 and q["source"] == "supplier_api"
        assert sent[0]["sku"] == sp.product.sku
        live_quote(s, sp)
        assert len(sent) == 1  # cached

    quotes._cache.clear()
    monkeypatch.setattr(quotes.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    with session_scope() as s:
        assert live_quote(s, s.scalar(select(SupplierProduct)))["source"] == "model"


# ------------------------------------------------------------------------------------------ embeddings
def test_embedding_provider_and_fallback(monkeypatch):
    base = rag.hash_embed("hello")
    assert len(base) == rag.EMBED_DIM
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "embedding_api_key", "k")
    vec = [0.0] * rag.EMBED_DIM
    vec[3] = 2.0
    monkeypatch.setattr(rag.httpx, "post", lambda *a, **k: FakeResp(200, {"data": [{"embedding": vec}]}))
    out = rag.embed("anything")
    assert out[3] == pytest.approx(1.0)  # remote vector used (and normalised)
    monkeypatch.setattr(rag.httpx, "post", lambda *a, **k: FakeResp(200, {"data": [{"embedding": [1.0, 2.0]}]}))
    assert rag.embed("hello") == base  # wrong dimension -> hash fallback
    monkeypatch.setattr(rag.httpx, "post", lambda *a, **k: FakeResp(500))
    assert rag.embed("hello") == base  # provider error -> hash fallback


def test_reindex_reembeds_contracts(client):
    with session_scope() as s:
        assert rag.reindex(s) > 0


# ---------------------------------------------------------------------------------------- LLM budget
class MeteredChat:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        content = json.dumps({"severity": "high", "title": "t", "summary": "s", "root_cause": "r", "confidence": 0.9})
        return type("Msg", (), {"content": content,
                                "usage_metadata": {"input_tokens": 1000, "output_tokens": 500}})()


def test_llm_usage_is_metered_and_budget_blocks_calls(client, monkeypatch):
    chat = MeteredChat()
    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(llm, "get_chat_model", lambda: chat)
    fb = Triage(severity="low", title="fb", summary="", root_cause="", confidence=0.1)

    _, source = llm.structured_call("sys", "u", Triage, lambda: fb)
    assert source == "deepseek" and chat.calls == 1
    with session_scope() as s:
        row = s.scalar(select(LLMUsage))
        assert (row.input_tokens, row.output_tokens) == (1000, 500)
        assert row.cost_usd == pytest.approx(llm.estimate_cost(1000, 500))

    monkeypatch.setattr(settings, "llm_daily_token_budget", 1000)  # already used 1500 today
    result, source = llm.structured_call("sys", "u", Triage, lambda: fb)
    assert source == "fallback" and result is fb and chat.calls == 1  # model NOT called

    monkeypatch.setattr(settings, "llm_daily_token_budget", 0)
    monkeypatch.setattr(settings, "llm_daily_budget_usd", 0.0000001)
    assert llm.structured_call("sys", "u", Triage, lambda: fb)[1] == "fallback"

    with session_scope() as s:  # yesterday's spend doesn't count
        for row in s.scalars(select(LLMUsage)).all():
            row.created_at = utcnow() - timedelta(days=2)
    assert llm.structured_call("sys", "u", Triage, lambda: fb)[1] == "deepseek"


def test_llm_outage_falls_back(client, monkeypatch):
    class Down:
        def invoke(self, m):
            raise httpx.ConnectError("llm down")

    monkeypatch.setattr(settings, "deepseek_api_key", "sk-test")
    monkeypatch.setattr(llm, "get_chat_model", lambda: Down())
    fb = Triage(severity="low", title="fb", summary="", root_cause="", confidence=0.1)
    assert llm.structured_call("sys", "u", Triage, lambda: fb) == (fb, "fallback")


# ----------------------------------------------------------------------------------------- feeds
def test_flightaware_and_vessel_parsing(monkeypatch):
    body = {"flights": [{"estimated_in": None, "scheduled_in": None}, {"estimated_in": "2026-12-01T10:00:00Z"}]}
    assert feeds.parse_flightaware(body).isoformat() == "2026-12-01T10:00:00"
    monkeypatch.setattr(settings, "vessel_tracking_eta_field", "data.0.voyage.eta")
    assert feeds.parse_vessel({"data": [{"voyage": {"eta": "2026-12-05T00:00:00+02:00"}}]}).isoformat() == \
        "2026-12-04T22:00:00"
    assert feeds.parse_vessel({"data": []}) is None


def test_poll_tracking_emits_only_material_eta_changes(client, monkeypatch):
    monkeypatch.setattr(settings, "flightaware_api_key", "k")
    monkeypatch.setattr(settings, "vessel_tracking_url", "http://vessels/{ref}")
    with session_scope() as s:
        ships = s.scalars(select(Shipment).order_by(Shipment.id)).all()
        for i, sh in enumerate(ships[:3]):
            sh.tracking_ref, sh.mode, sh.status = f"REF{i}", ("air" if i == 0 else "ocean"), "in_transit"
        codes = [sh.code for sh in ships[:3]]
        etas = [sh.eta for sh in ships[:3]]
    new_etas = {"REF0": etas[0] + timedelta(days=3), "REF1": etas[1] + timedelta(minutes=10), "REF2": None}
    with session_scope() as s:
        out = feeds.poll_tracking(s, fetch_air=lambda ref: new_etas[ref], fetch_ocean=lambda ref: new_etas[ref])
    assert [o["shipment_code"] for o in out] == [codes[0]]  # +10 min ignored, missing ETA ignored
    assert out[0]["source"] == "flightaware"


def test_poll_tracking_survives_provider_errors(client, monkeypatch):
    monkeypatch.setattr(settings, "vessel_tracking_url", "http://vessels/{ref}")
    with session_scope() as s:
        sh = s.scalar(select(Shipment))
        sh.tracking_ref, sh.mode = "X", "ocean"

    def boom(ref):
        raise httpx.ConnectError("provider down")

    with session_scope() as s:
        assert feeds.poll_tracking(s, fetch_ocean=boom) == []


def test_mqtt_message_parsing():
    ok = feeds.parse_mqtt_message("supplychain/telemetry/temperature_c",
                                  b'{"shipment_code": "SHP-1006", "value": "47.5", "lat": 1.5}')
    assert ok == {"shipment_code": "SHP-1006", "metric": "temperature_c", "value": 47.5, "lat": 1.5}
    explicit = feeds.parse_mqtt_message("t/x", b'{"metric": "shock_g", "value": 9}')
    assert explicit["metric"] == "shock_g"
    assert feeds.parse_mqtt_message("t", b"not json") is None
    assert feeds.parse_mqtt_message("t", b'{"metric": "m"}') is None


# ------------------------------------------------------------------------------------- migrations
def test_migrations_match_models(client):
    """Fails when someone changes models.py without adding an Alembic migration."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db import Base, engine

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"models differ from migrations: {diff}"
