"""Failure-mode tests: infrastructure outages must degrade gracefully, never crash the pipeline."""
import asyncio

import pytest

import app.events as events
from app.config import settings
from app.events import InMemoryBus, RedisStreamBus, make_event


@pytest.fixture(autouse=True)
def reset_bus():
    events._bus = None
    yield
    events._bus = None


def test_unreachable_redis_falls_back_to_in_memory_bus(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")  # nothing listens on port 1
    bus = asyncio.run(events.get_bus())
    assert isinstance(bus, InMemoryBus)


def test_failing_handler_does_not_kill_the_consumer_loop():
    async def scenario():
        bus = InMemoryBus()
        seen = []

        async def handler(event):
            seen.append(event["event_id"])
            if len(seen) == 1:
                raise RuntimeError("boom")

        task = asyncio.create_task(bus.consume(handler, "c1"))
        for _ in range(2):
            await bus.publish(make_event("x", {}))
        for _ in range(50):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        return seen

    assert len(asyncio.run(scenario())) == 2  # second event still processed after the first one crashed


def test_redis_bus_redelivers_unacked_entries_after_crash():
    """Entries a consumer read but never acked are re-read on restart (pending-entries pass)."""
    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def xgroup_create(self, *a, **k):
            return True

        async def xreadgroup(self, group, consumer, streams, count=None, block=None):
            self.calls.append(list(streams.values())[0])
            if list(streams.values())[0] == "0":  # pending (un-acked) pass
                return [("supply.events", [("1-0", {"data": '{"event_id": "e1", "type": "t"}'})])]
            raise asyncio.CancelledError  # stop after the first live read

        async def xack(self, *a):
            self.acked = a

    async def scenario():
        bus = RedisStreamBus.__new__(RedisStreamBus)
        bus.r, bus.stream, bus.group = FakeRedis(), "supply.events", "g"
        handled = []

        async def handler(ev):
            handled.append(ev["event_id"])

        with pytest.raises(asyncio.CancelledError):
            await bus.consume(handler, "worker-1")
        return handled, bus.r

    handled, r = asyncio.run(scenario())
    assert handled == ["e1"] and r.calls[0] == "0" and r.acked[2] == "1-0"
