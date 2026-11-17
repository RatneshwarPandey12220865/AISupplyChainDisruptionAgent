import os
import tempfile

# Configure an isolated SQLite database and force the deterministic (no-LLM) path before app imports.
_tmp = tempfile.mkdtemp(prefix="scda-test-")
os.environ.update({
    "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db",
    "CHECKPOINT_SQLITE_PATH": f"{_tmp}/checkpoints.db",
    "DEEPSEEK_API_KEY": "",
    "REDIS_URL": "",
    "RUN_EMBEDDED_WORKER": "false",
    "MONTE_CARLO_RUNS": "3000",
    "ENVIRONMENT": "development",
    "API_KEYS": "",
    "WEBHOOK_SECRET": "",
    "LLM_DAILY_BUDGET_USD": "0",
    "ERP_WEBHOOK_URL": "",
    "SUPPLIER_QUOTE_URL": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.worker as _worker  # noqa: E402
from app.config import settings  # noqa: E402
from app.security import limiter  # noqa: E402
from app.seed import reset_database  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    reset_database()
    limiter.hits.clear()
    yield


class SinkBus:
    """Captures published events so tests can process them synchronously."""
    name = "test"

    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)

    async def close(self):
        pass


@pytest.fixture
def bus(monkeypatch):
    sink = SinkBus()

    async def get_bus():
        return sink

    monkeypatch.setattr(_worker, "get_bus", get_bus)
    return sink


@pytest.fixture
def client(bus):
    from app.main import app

    with TestClient(app) as c:
        yield c


KEYS = {"viewer": "viewer-key-0123456789", "approver": "approver-key-0123456789", "admin": "admin-key-0123456789"}


@pytest.fixture
def secured(monkeypatch):
    """Enable API-key auth + webhook secret."""
    monkeypatch.setattr(settings, "api_keys", ",".join(f"{r}-user:{k}:{r}" for r, k in KEYS.items()))
    monkeypatch.setattr(settings, "webhook_secret", "whsec_test_secret")


def auth(role: str) -> dict:
    return {"X-API-Key": KEYS[role]}
