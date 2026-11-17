"""Event bus: Redis Streams (consumer groups, at-least-once) with an in-memory fallback for local dev."""
import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from app.config import redact_url, settings
from app.models import utcnow

log = logging.getLogger(__name__)
Handler = Callable[[dict], Awaitable[None]]


def make_event(type_: str, payload: dict, source: str = "api") -> dict:
    return {"event_id": uuid.uuid4().hex, "type": type_, "source": source,
            "payload": payload, "ts": utcnow().isoformat()}


class EventBus(ABC):
    name = "abstract"

    @abstractmethod
    async def publish(self, event: dict) -> None: ...

    @abstractmethod
    async def consume(self, handler: Handler, consumer_name: str) -> None: ...

    async def close(self) -> None:
        pass


class InMemoryBus(EventBus):
    name = "memory"

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue()

    async def publish(self, event: dict) -> None:
        await self.queue.put(event)

    async def consume(self, handler: Handler, consumer_name: str) -> None:
        while True:
            event = await self.queue.get()
            try:
                await handler(event)
            except Exception:
                log.exception("Handler failed for event %s", event.get("event_id"))


class RedisStreamBus(EventBus):
    name = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self.r = aioredis.from_url(url, decode_responses=True)
        self.stream = settings.event_stream
        self.group = settings.consumer_group

    async def ping(self) -> bool:
        return bool(await self.r.ping())

    async def publish(self, event: dict) -> None:
        await self.r.xadd(self.stream, {"data": json.dumps(event, default=str)}, maxlen=100_000)

    async def consume(self, handler: Handler, consumer_name: str) -> None:
        try:
            await self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP: group already exists
            if "BUSYGROUP" not in str(exc):
                raise
        async def handle(resp) -> None:
            for msg_id, fields in (resp[0][1] if resp else []):
                try:
                    await handler(json.loads(fields["data"]))
                    await self.r.xack(self.stream, self.group, msg_id)
                except Exception:
                    log.exception("Handler failed for stream entry %s", msg_id)

        # One pass over entries this consumer left un-acked (crash recovery), then block for new ones.
        await handle(await self.r.xreadgroup(self.group, consumer_name, {self.stream: "0"}, count=100))
        while True:
            await handle(await self.r.xreadgroup(self.group, consumer_name, {self.stream: ">"},
                                                 count=10, block=5000))

    async def close(self) -> None:
        await self.r.aclose()


_bus: EventBus | None = None


async def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        if settings.redis_url:
            try:
                bus = RedisStreamBus(settings.redis_url)
                await bus.ping()
                _bus = bus
                log.info("Event bus: Redis Streams at %s", redact_url(settings.redis_url))
            except Exception as exc:
                if settings.is_production:
                    # An in-memory bus per process would silently drop events between the API and the workers.
                    raise RuntimeError(f"Redis unreachable at {redact_url(settings.redis_url)}: {exc}") from exc
                log.warning("Redis unavailable (%s); falling back to in-memory bus", exc)
        if _bus is None:
            _bus = InMemoryBus()
            log.info("Event bus: in-memory")
    return _bus
