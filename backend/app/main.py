"""FastAPI application: ingestion webhooks, dashboard API and the embedded agent worker."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import ingest, routes
from app.config import settings
from app.db import init_db, session_scope
from app.events import get_bus
from app.seed import seed
from app.worker import run_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as s:
        if seed(s):
            log.info("Seeded demo data")
    if not settings.llm_enabled:
        log.warning("DEEPSEEK_API_KEY not set: agents will use deterministic fallbacks instead of DeepSeek")
    worker = asyncio.create_task(run_worker()) if settings.run_embedded_worker else None
    yield
    if worker:
        worker.cancel()
    await (await get_bus()).close()


app = FastAPI(title="AI Supply Chain Disruption Agent", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(ingest.router)
app.include_router(routes.router)
