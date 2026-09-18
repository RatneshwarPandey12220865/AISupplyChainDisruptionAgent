from fastapi.testclient import TestClient

import app.worker as worker
from app.main import app
from app.worker import process_event


class SinkBus:
    """Captures published events so the test can process them synchronously."""
    name = "test"

    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def test_api_flow(monkeypatch):
    bus = SinkBus()

    async def get_bus():
        return bus

    monkeypatch.setattr(worker, "get_bus", get_bus)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"

        r = client.post("/api/ingest/shipment/eta", json={"shipment_code": "SHP-1002", "delay_days": 10})
        assert r.status_code == 200
        process_event(bus.events[-1])

        items = client.get("/api/disruptions").json()
        assert items and items[0]["status"] == "awaiting_approval"
        did = items[0]["id"]
        detail = client.get(f"/api/disruptions/{did}").json()
        assert detail["simulation"]["histogram"]["counts"]
        assert detail["recommendation"]["urgency"]["headline"]

        r = client.post(f"/api/disruptions/{did}/decision", json={"approved": True, "reviewer": "bob"})
        assert r.status_code == 200 and r.json()["status"] == "approved_executed"
        assert client.post(f"/api/disruptions/{did}/decision", json={"approved": True}).status_code == 409

        po = client.get("/api/purchase-orders").json()[0]
        assert client.post(f"/api/purchase-orders/{po['id']}/receive", json={"actual_days": 5}).status_code == 200
        assert client.post(f"/api/disruptions/{did}/resolve", json={}).status_code == 200
        assert client.get(f"/api/disruptions/{did}").json()["status"] == "resolved"

        assert client.post("/api/ingest/erp/inventory",
                           json={"sku": "BAD", "warehouse": "DAL", "on_hand": 1}).status_code == 422
        assert client.get("/api/dashboard").json()["kpis"]["resolved"] == 1
