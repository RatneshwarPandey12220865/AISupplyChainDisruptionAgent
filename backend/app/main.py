"""FastAPI application: ingestion webhooks, dashboard API and the embedded agent worker."""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app import metrics
from app.api import auth, ingest, routes
from app.config import settings
from app.db import init_db, session_scope
from app.events import get_bus
from app.logging_setup import request_id_var, setup_logging
from app.seed import seed
from app.worker import run_worker

setup_logging()
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for warning in settings.validate_for_runtime():  # raises in production when misconfigured
        log.warning(warning)
    init_db()
    if settings.seed_enabled:
        with session_scope() as s:
            if seed(s):
                log.info("Seeded demo data")
    else:
        log.info("Demo seeding disabled (SEED_DEMO_DATA off / production)")
    if not settings.llm_enabled:
        log.warning("DEEPSEEK_API_KEY not set: agents will use deterministic fallbacks instead of DeepSeek")
    worker = asyncio.create_task(run_worker()) if settings.run_embedded_worker else None
    yield
    if worker:
        worker.cancel()
    await (await get_bus()).close()


app = FastAPI(title="AI Supply Chain Disruption Agent", version="1.1.0", lifespan=lifespan,
              docs_url=None if settings.is_production else "/docs",
              redoc_url=None, openapi_url=None if settings.is_production else "/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                   allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
                   allow_headers=["Content-Type", "Authorization", "X-API-Key", "Idempotency-Key"],
                   expose_headers=["X-Request-ID"])


@app.middleware("http")
async def observe(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id_var.set(rid)
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
        metrics.HTTP_LATENCY.labels(request.method, route).observe(time.monotonic() - started)
        request_id_var.reset(token)


# --- public, unauthenticated: liveness probe and Prometheus scrape --------------------------------------------
public = APIRouter()


@public.get("/api/health")
def health():
    """Liveness for load balancers/Docker. Deliberately reveals nothing about configuration (see /api/status)."""
    return {"status": "ok"}


@public.get("/metrics", include_in_schema=False)
def prometheus(request: Request):
    if not settings.metrics_enabled:
        raise HTTPException(404)
    if settings.metrics_token and request.headers.get("authorization", "") != f"Bearer {settings.metrics_token}":
        raise HTTPException(401, "Metrics token required")
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)


app.include_router(public)
app.include_router(auth.router)
app.include_router(auth.admin)
app.include_router(ingest.router)
app.include_router(routes.router)
