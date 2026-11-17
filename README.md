# AI Supply Chain Disruption Agent

An event-driven, multi-agent system that ingests supply-chain telemetry, detects disruptions, quantifies
their impact with Monte Carlo simulation, evaluates mitigations, and either **auto-executes** low-risk
recovery plans or **halts for human approval**. Built with LangGraph and **DeepSeek** (through its
OpenAI-compatible API).

```
Ingestion (FastAPI, signed webhooks) ──> Event bus (Redis Streams | in-memory) ──> Agent worker
                                                                                     │
  LangGraph:  detection ─> impact ─> monte-carlo ─> sourcing ─> guardrail ─┬─> execution (ERP write-back + outbox retries)
                 │                                                          ├─> monitor
                 └─> END (duplicate)                                        └─> escalate ─> human_review (interrupt)
                                                                                              ├─> execution
                                                                                              └─> reject
  Background: post-mortems + supplier scoring · signal expiry · ERP outbox · weather/news/carrier polling · MQTT bridge
```

| Layer | Implementation |
|---|---|
| Agent orchestration | LangGraph `StateGraph`, durable checkpoints (`PostgresSaver` / `SqliteSaver`), `interrupt()` for HITL |
| LLM | DeepSeek (`deepseek-chat`) via `langchain-openai`, JSON-mode outputs validated by Pydantic, metered + daily budget cap |
| API | FastAPI: API-key auth with roles, HMAC-signed webhooks, rate limiting, SSE live updates, Prometheus `/metrics` |
| Relational + time series | PostgreSQL + TimescaleDB hypertable for telemetry (SQLite for local dev), **Alembic** migrations |
| Vector search (RAG) | pgvector over supplier contracts/SLAs; pluggable embedding provider (offline hash or OpenAI-compatible) |
| Queue / state | Redis Streams consumer groups with idempotent event claiming (in-memory fallback) |
| UI | Next.js 15 + Tailwind CSS v4, sign-in, live updates over SSE |

## The agents

1. **Detection** (`backend/app/agents/detection.py`). A rules engine checks every event for below-reorder stock,
   lead-time jumps, ETA slips, sensor excursions, supplier outages and regional risk signals. DeepSeek then triages
   the anomaly. The LLM can raise the rules engine's severity but never lower it. Duplicate signals are merged.
2. **Impact** (`impact.py`). Deterministic stockout window, orders at risk (VIP, late penalties) and dollar exposure;
   the LLM only writes the narrative.
3. **Monte Carlo simulation** (`risk_simulation.py`, `simulation.py`). 10k simulations of demand and arrival delay;
   expected/P15/P50/P85/P95 loss and P(loss).
4. **Sourcing & Action** (`sourcing.py`). Wait / expedite / alternative-supplier PO / inter-warehouse transfer options,
   each simulated with common random numbers, a cost-of-waiting curve, contract clauses via RAG, and an LLM-chosen plan.
   Prices come from your supplier pricing API when `SUPPLIER_QUOTE_URL` is set (else a drift model).
5. **Guardrails** (`guardrails.py`). Auto-execute only if cost ≤ `AUTO_APPROVE_LIMIT`, no VIP orders, severity not
   critical, supplier reliability ≥ 0.70 and LLM confidence ≥ 0.5. Otherwise planners are alerted and the graph pauses.
6. **Execution** (`execution.py`, `services/erp.py`). Writes POs/expedites/transfers to the ERP with idempotency keys,
   exponential-backoff retries and a persistent outbox (see *ERP integration*).
7. **Post-mortem & scoring** (`services/postmortem.py`). Compares actual recovery to plan and updates supplier
   reliability (EWMA); scores feed later sourcing decisions and guardrails.

---

## Run it locally (step by step)

Prerequisites: **Python 3.12+** and **Node.js 20+**. No Docker, Postgres or Redis needed for local development
(SQLite + an in-memory event bus are used automatically).

### 1. Configure

```bash
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
```

For a first run you can leave everything as is: `ENVIRONMENT=development`, no API keys (auth disabled), demo data on.
Optionally set `DEEPSEEK_API_KEY=sk-...` to use the real LLM; without it every agent uses deterministic fallbacks.

### 2. Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

On first start the database is created by Alembic migrations and demo data is seeded. Check
http://localhost:8000/api/health (`{"status":"ok"}`) and the interactive docs at http://localhost:8000/docs.

> Upgrading from an earlier checkout? An existing `supplychain.db` created before migrations existed is adopted
> automatically (missing columns are added and the DB is stamped). If anything looks off, delete
> `backend/supplychain.db*` and `backend/checkpoints.db*` and restart.

### 3. Frontend (second terminal)

```bash
cd frontend
npm install
npm run dev                       # http://localhost:3000
```

Open http://localhost:3000. In development the UI points at `http://localhost:8000` by default.

### 4. Try it

- Click **Vessel rollover (+10d)**: the critical plan needs approval. Open it from the table and **Approve & execute**.
- Click **Low stock at Dallas**: auto-executes a small transfer.
- On an order, **Mark received** and the post-mortem runs and updates supplier scores (**Suppliers** page).
- The dashboard updates live (Server-Sent Events); no refresh needed.

### 5. Try it with authentication on (recommended before deploying)

Set these in `.env`, restart the backend, and reload the UI. You'll get a sign-in screen:

```ini
API_KEYS=alice:alice-secret-key-123456:approver,bob:bob-secret-key-1234567:viewer,root:root-secret-key-1234567:admin
WEBHOOK_SECRET=local-webhook-secret
```

- `viewer` can read, `approver` can approve/reject/receive/resolve/inject demo scenarios, `admin` can also reset the demo DB.
- Approvals record the **API key's owner** (e.g. `alice`) as the reviewer; the request body can't spoof it.

Send a signed webhook:

```bash
BODY='{"shipment_code":"SHP-1002","delay_days":10}'
curl -X POST http://localhost:8000/api/ingest/shipment/eta \
  -H "Content-Type: application/json" -H "X-Webhook-Secret: local-webhook-secret" -d "$BODY"

# or HMAC (preferred; add X-Timestamp for replay protection, signed as "<ts>.<body>")
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'local-webhook-secret' | sed 's/^.* //')
curl -X POST http://localhost:8000/api/ingest/shipment/eta -H "Content-Type: application/json" \
  -H "X-Signature: sha256=$SIG" -d "$BODY"
```

Add an `Idempotency-Key: <unique id>` header and replays of the same key return `"status":"duplicate"` instead of a second event.

## Testing

```bash
# Backend: 50+ tests (pipeline, auth/RBAC, signatures, idempotency, ERP retries, budgets, feeds, migrations, outages)
cd backend && .venv\Scripts\python -m pytest -q
cd backend && .venv\Scripts\python -m ruff check app tests migrations

# Frontend
cd frontend && npm run typecheck && npm test

# End-to-end (Playwright): start the backend with keys, the frontend, then:
#   backend/.env or env:  API_KEYS=alice:e2e-approver-key-0123456789:approver,vic:e2e-viewer-key-0123456789:viewer
cd frontend && npx playwright install chromium && npm run test:e2e

# Load test (needs a running API): pip install locust
cd backend && API_KEY=... WEBHOOK_SECRET=... locust -f loadtest/locustfile.py --host http://localhost:8000
```

PostgreSQL run of the suite: `TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest -q` (CI does this against `pgvector/pgvector`).
GitHub Actions (`.github/workflows/ci.yml`) runs lint, backend tests (SQLite and Postgres), frontend typecheck/tests/build, Playwright e2e and Docker builds.

---

## Full stack (Docker Compose)

```bash
cp .env.example .env
# Required in .env:  ENVIRONMENT=production  POSTGRES_PASSWORD  REDIS_PASSWORD  API_KEYS  WEBHOOK_SECRET
# For a quick local look at demo data also set:  SEED_DEMO_DATA=true  (and ALLOW_ADMIN_RESET=true)
docker compose up -d --build
# UI http://localhost:3000 · API http://localhost:8000
docker compose up -d --scale worker=3     # scale agent workers on the Redis stream
```

Compose refuses to start without the required secrets. Postgres and Redis are **not published** to the host; the API
and UI listen on `127.0.0.1` only (set `BIND_ADDRESS=0.0.0.0` if you don't use a host reverse proxy). In production put
Caddy/nginx/Traefik in front for TLS. The browser-facing API URL is the **runtime** variable `PUBLIC_API_URL` (passed to the
frontend as `API_URL`; no rebuild per domain) and `CORS_ORIGINS` must be your dashboard origin.

Production mode (`ENVIRONMENT=production`) refuses to boot without API keys (≥16 chars) and a webhook secret, rejects
`CORS_ORIGINS=*`, hides `/docs`, and turns demo seeding and `/api/admin/reset` off.

## Supabase (Postgres) + hosted Redis

Use this instead of the bundled Postgres/Redis containers.

1. **Supabase**: create a project, then *Connect* → copy the **session pooler** string (port 5432). Put it in `.env` as
   `DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`
   (`postgresql://` is rewritten for psycopg 3; URL-encode special characters in the password). `pgvector` is enabled
   automatically by the first migration. Supabase has **no TimescaleDB**: the `telemetry_readings` table is created as a
   plain table (logged as a warning, harmless at this scale). The transaction pooler (port 6543) also works, but
   the migration lock is skipped, so start the API before the workers on first boot.
2. **Redis**: Supabase does not offer Redis. Use Upstash, Redis Cloud or similar with **Streams** support and set
   `REDIS_URL=rediss://default:<password>@<host>:6379` (`rediss://` = TLS). In production the app refuses to start if Redis
   is unreachable, rather than silently falling back to a per-process in-memory bus.
3. Run only the app services: `docker compose -f docker-compose.cloud.yml up -d --build` (same required secrets, plus
   `DATABASE_URL` and `REDIS_URL`).

For a local (non-Docker) run against Supabase, put the same two variables in `backend/.env` and start as in step 2 above.
Use a separate Supabase project for development: the first start creates the schema, and `ALLOW_ADMIN_RESET` drops tables.

## Configuration highlights

| Area | Variables |
|---|---|
| Auth | `API_KEYS` (`name:key:role`), `WEBHOOK_SECRET`, `WEBHOOK_ALLOW_PLAIN_SECRET`, `WEBHOOK_MAX_SKEW_SECONDS` |
| Safety | `SEED_DEMO_DATA`, `ALLOW_ADMIN_RESET`, `RATE_LIMIT_PER_MINUTE`, `INGEST_RATE_LIMIT_PER_MINUTE` |
| LLM cost | `LLM_DAILY_BUDGET_USD`, `LLM_DAILY_TOKEN_BUDGET`, `LLM_PRICE_PER_M_INPUT_USD`, `LLM_PRICE_PER_M_OUTPUT_USD` |
| RAG | `EMBEDDING_PROVIDER` (`hash`/`openai`), `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBED_DIM` |
| Pricing | `SUPPLIER_QUOTE_URL`, `SUPPLIER_QUOTE_API_KEY` |
| ERP | `ERP_WEBHOOK_URL`, `ERP_FLAVOR` (`generic`/`sap`/`netsuite`), `ERP_AUTH_HEADER`, `ERP_AUTH_VALUE`, `ERP_MAX_ATTEMPTS` |
| Feeds | `ENABLE_TRACKING_POLLING`, `FLIGHTAWARE_API_KEY`, `VESSEL_TRACKING_URL/_API_KEY/_ETA_FIELD`, `MQTT_URL`, `ENABLE_EXTERNAL_POLLING` |
| Ops | `LOG_FORMAT=json`, `METRICS_TOKEN`, `WORKER_METRICS_PORT` |

## Ingestion API

All `/api/ingest/*` calls need a webhook signature (or an approver/admin API key) when `WEBHOOK_SECRET` is set.

| Endpoint | Event | Example body |
|---|---|---|
| `POST /api/ingest/erp/inventory` | `inventory.update` | `{"sku":"CON-USBC","warehouse":"DAL","on_hand":1200}` |
| `POST /api/ingest/erp/order` | `order.created` | `{"order_number":"ORD-9","customer":"X","is_vip":true,"sku":"MCU-32","warehouse":"DAL","quantity":500,"due_date":"2026-10-01T00:00:00"}` |
| `POST /api/ingest/supplier` | `supplier.update` | `{"supplier_id":3,"sku":"PSU-65W","lead_time_days":26}` or `{"supplier_id":5,"status":"force_majeure"}` |
| `POST /api/ingest/shipment/eta` | `shipment.eta_update` | `{"shipment_code":"SHP-1002","delay_days":10}` |
| `POST /api/ingest/telemetry` | `telemetry.reading` | `{"shipment_code":"SHP-1006","metric":"temperature_c","value":47.5}` |
| `POST /api/ingest/external` | `external.signal` | `{"kind":"strike","region":"Europe","severity":0.6}` |

Dashboard API (API key, `X-API-Key` or `Authorization: Bearer`): `GET /api/me`, `/api/status`, `/api/dashboard`,
`/api/disruptions`, `/api/stream` (SSE) and more (see `/docs` in development). Actions: `POST /api/disruptions/{id}/decision`
(`approver`), `/api/disruptions/{id}/resolve`, `/api/purchase-orders/{id}/receive`, `/api/simulate/{scenario}` (demo mode only),
`/api/admin/reset` (`admin`, development only by default). `GET /api/health` is public and minimal; `GET /metrics` is Prometheus.

## ERP integration

`erp.py` writes every PO locally first, then pushes to `ERP_WEBHOOK_URL` with `Idempotency-Key: <po_number>`.
Transient failures (timeout, 429, 5xx) retry inline with exponential backoff; if the ERP is still down the PO stays
`erp_status=pending` and the worker's background loop retries until `ERP_RETRY_MAX_TOTAL_ATTEMPTS`, then marks it `failed`
and raises a critical alert. Permanent 4xx errors fail immediately. `ERP_FLAVOR` selects the payload mapping. **The `sap`
and `netsuite` mappings are starting templates**: adjust field names to your tenant's configuration before going live.

## Live data feeds

- **Weather / news**: `ENABLE_EXTERNAL_POLLING` (Open-Meteo needs no key; NewsAPI optional).
- **Carrier tracking**: `ENABLE_TRACKING_POLLING` polls shipments that have a `tracking_ref`: FlightAware AeroAPI for air, and
  any vessel API via `VESSEL_TRACKING_URL` (with `{ref}`) + `VESSEL_TRACKING_ETA_FIELD` for ocean. ETA moves over an hour
  become `shipment.eta_update` events through the normal pipeline.
- **IoT**: set `MQTT_URL`; JSON messages `{"shipment_code","metric","value","lat","lon"}` on `MQTT_TOPIC` become telemetry events.

> The feed adapters follow the providers' public docs and are unit-tested with sample payloads, but have **not been run
> against live provider accounts**. Verify with your own credentials (and set `shipments.tracking_ref`) before relying on them.

## Database migrations

Schema is managed by Alembic and applied automatically at start-up. After changing `app/models.py`:

```bash
cd backend && alembic revision --autogenerate -m "describe change" && alembic upgrade head
```

A test fails if models and migrations drift. Changing `EMBED_DIM` also needs a pgvector column migration, then `python -m app.rag reindex`.

## Observability

- `/metrics` (Prometheus): HTTP, auth failures, rate limits, events, agent run time, `llm_calls_total`, `llm_tokens_total`,
  `llm_cost_usd_total`, `llm_call_duration_seconds`, ERP sync results, feed polls, SSE clients. Standalone workers expose theirs
  on `WORKER_METRICS_PORT`.
- Per-call LLM usage and estimated cost are stored in the `llm_usage` table; `LLM_DAILY_BUDGET_USD` switches agents to
  deterministic fallbacks once the day's spend is reached.
- `LOG_FORMAT=json` emits structured logs with request ids (`X-Request-ID`).
- Not included: distributed tracing (OpenTelemetry). Add it at the reverse proxy/ASGI layer if you need it.

## Notes & limits

- The rate limiter is per process and in-memory: with several API replicas also limit at your gateway.
- Prices are simulated unless `SUPPLIER_QUOTE_URL` is configured. Hashed embeddings are the offline default.
- The PostgreSQL/TimescaleDB path is covered by a CI job but was not exercised on the author's machine (no Docker locally).
