"""Load test: dashboard reads + a stream of ingestion webhooks (signed) against a running API.

    pip install locust
    API_KEY=... WEBHOOK_SECRET=... locust -f loadtest/locustfile.py --host http://localhost:8000

Watch `llm_calls_total`, `rate_limited_total` and `events_total` on /metrics while it runs. With a DeepSeek key
configured, set LLM_DAILY_BUDGET_USD first so a flood can't run up a bill.
"""
import hashlib
import hmac
import json
import os
import random
import time

from locust import HttpUser, between, task

API_KEY = os.environ.get("API_KEY", "")
SECRET = os.environ.get("WEBHOOK_SECRET", "")


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if SECRET:
        ts = str(time.time())
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, headers


class Planner(HttpUser):
    weight = 3
    wait_time = between(1, 3)

    def on_start(self):
        self.client.headers.update({"X-API-Key": API_KEY} if API_KEY else {})

    @task(4)
    def dashboard(self):
        self.client.get("/api/dashboard")

    @task(2)
    def disruptions(self):
        self.client.get("/api/disruptions")

    @task(1)
    def suppliers(self):
        self.client.get("/api/suppliers")


class Webhooks(HttpUser):
    weight = 1
    wait_time = between(0.1, 0.5)

    @task(3)
    def inventory(self):
        raw, headers = signed({"sku": "CON-USBC", "warehouse": "DAL", "on_hand": random.randint(900, 3000)})
        self.client.post("/api/ingest/erp/inventory", data=raw, headers=headers, name="/api/ingest/erp/inventory")

    @task(1)
    def telemetry(self):
        raw, headers = signed({"shipment_code": "SHP-1006", "metric": "temperature_c", "value": random.uniform(2, 9)})
        self.client.post("/api/ingest/telemetry", data=raw, headers=headers, name="/api/ingest/telemetry")
