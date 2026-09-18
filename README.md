# AI Supply Chain Disruption Agent

An event-driven, multi-agent system that ingests supply-chain telemetry, detects disruptions, quantifies
their impact with Monte Carlo simulation, evaluates mitigations, and either **auto-executes** low-risk
recovery plans or **halts for human approval**. Built with LangGraph and **DeepSeek** (through its
OpenAI-compatible API).

```
Ingestion (FastAPI) ──> Event bus (Redis Streams | in-memory) ──> Agent worker
                                                                     │
  LangGraph:  detection ─> impact ─> monte-carlo ─> sourcing ─> guardrail ─┬─> execution (ERP write-back)
                 │                                                          ├─> monitor
                 └─> END (duplicate)                                        └─> escalate ─> human_review (interrupt)
                                                                                              ├─> execution
                                                                                              └─> reject
  Background: post-mortems + supplier scoring · signal expiry · weather/news polling
```

| Layer | Implementation |
|---|---|
| Agent orchestration | LangGraph `StateGraph`, durable checkpoints (`PostgresSaver` / `SqliteSaver`), `interrupt()` for HITL |
| LLM | DeepSeek (`deepseek-chat`) via `langchain-openai`, JSON-mode structured outputs validated by Pydantic |
| API | FastAPI (ingestion webhooks, dashboard API, approvals) |
| Relational + time series | PostgreSQL + TimescaleDB hypertable for telemetry (SQLite for local dev) |
| Vector search (RAG) | pgvector over supplier contracts/SLAs (numpy cosine on SQLite) |
| Queue / state | Redis Streams consumer groups (in-memory fallback) |
| UI | Next.js 15 + Tailwind CSS v4 |

## The agents

1. **Detection** (`backend/app/agents/detection.py`). A rules engine checks every event for
   below-reorder stock, lead-time jumps, ETA slips, sensor excursions (temperature, shock, humidity),
   supplier outages, and regional risk signals. DeepSeek then triages the anomaly: severity, headline,
   and root cause. The LLM can raise the rules engine's severity but never lower it. Duplicate signals
   are merged into the open disruption.
2. **Impact** (`impact.py`). Queries inventory, inbound shipments, and open orders to project the
   stockout window, the orders at risk (VIP flags, late penalties), and the dollar exposure. The numbers
   are deterministic; DeepSeek only writes the narrative.
3. **Monte Carlo simulation** (`risk_simulation.py`, `simulation.py`). Runs 10k simulations of demand
   (normal) and arrival delay (gamma, inflated by regional risk and supplier history). It reports
   expected/P15/P50/P85/P95 loss and P(loss).
4. **Sourcing & Action** (`sourcing.py`). Builds the candidate options: wait, expedite, POs from
   alternative suppliers at live quotes, and inter-warehouse transfers. Each option runs through the same
   simulation with common random numbers, which gives the residual loss and the expected total cost.
   The agent also computes a cost-of-waiting curve, for example *"85% probability of ≥ $45,378 loss if
   'Expedite with Hanoi Assembly Co' is not engaged within 12 hours"*. It pulls contract clauses through
   RAG, and DeepSeek picks and justifies the plan. An unknown option ID falls back to the cheapest
   option.
5. **Guardrails** (`guardrails.py`). A plan auto-executes only if all of these hold: cost ≤
   `AUTO_APPROVE_LIMIT` ($2,000), no VIP orders affected, severity not critical, supplier reliability
   ≥ 0.70, and LLM confidence ≥ 0.5. Anything else sends an alert (dashboard, plus Slack/email if
   configured) and pauses the graph until a planner approves, rejects, or overrides the option.
6. **Execution** (`execution.py`). Writes POs, expedite requests, and transfers to the ERP. Orders are
   stored locally and POSTed to `ERP_WEBHOOK_URL` if it is set.
7. **Post-mortem & scoring** (`services/postmortem.py`). When all of a disruption's orders are
   received (or a planner resolves it), the agent compares actual recovery against the plan and updates
   supplier reliability with an EWMA. DeepSeek writes the lessons learned. The new scores feed the next
   sourcing decisions and the reliability guardrail.

## Quick start (local, no Docker)

```bash
# 1. Configure DeepSeek
cp .env.example .env          # then set DEEPSEEK_API_KEY=sk-...

# 2. Backend (SQLite + in-memory bus, agent worker embedded)
cd backend
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 3. Frontend
cd ../frontend
npm install
npm run dev                   # http://localhost:3000
```

Open http://localhost:3000 and click one of the **Inject a disruption** buttons. You can:
- **Low stock at Dallas**: auto-executes a $255 transfer from Newark.
- **Vessel rollover**: escalates (VIP order, critical). Approve it from the disruption page.
- **Mark received** on the order, and the post-mortem runs and updates supplier scores
  (see **Suppliers**).

Without `DEEPSEEK_API_KEY` the full pipeline still runs on deterministic fallbacks. `/api/health` shows
which mode is active.

## Full stack (Docker)

```bash
cp .env.example .env   # set DEEPSEEK_API_KEY
docker compose up --build
# UI http://localhost:3000 · API docs http://localhost:8000/docs
docker compose up --scale worker=3   # scale agent workers on the Redis stream
```

## Ingestion API

| Endpoint | Event | Example body |
|---|---|---|
| `POST /api/ingest/erp/inventory` | `inventory.update` | `{"sku":"CON-USBC","warehouse":"DAL","on_hand":1200}` |
| `POST /api/ingest/erp/order` | `order.created` | `{"order_number":"ORD-9","customer":"X","is_vip":true,"sku":"MCU-32","warehouse":"DAL","quantity":500,"due_date":"2026-10-01T00:00:00"}` |
| `POST /api/ingest/supplier` | `supplier.update` | `{"supplier_id":3,"sku":"PSU-65W","lead_time_days":26}` or `{"supplier_id":5,"status":"force_majeure"}` |
| `POST /api/ingest/shipment/eta` | `shipment.eta_update` | `{"shipment_code":"SHP-1002","delay_days":10}` (VesselFinder/FlightAware webhooks go here) |
| `POST /api/ingest/telemetry` | `telemetry.reading` | `{"shipment_code":"SHP-1006","metric":"temperature_c","value":47.5}` |
| `POST /api/ingest/external` | `external.signal` | `{"kind":"strike","region":"Europe","severity":0.6}` |

Other endpoints:
- `POST /api/disruptions/{id}/decision` with `{"approved":true,"option_id":"po-6","reviewer":"alice"}`
- `POST /api/purchase-orders/{id}/receive`
- `POST /api/disruptions/{id}/resolve`
- `POST /api/simulate/{scenario}`
- `POST /api/admin/reset`

Full OpenAPI docs are at `/docs`.

External polling (`ENABLE_EXTERNAL_POLLING=true`) checks the Open-Meteo forecast at every supplier
location (no key needed). If `NEWSAPI_KEY` is set, it also has DeepSeek classify port, strike, and
closure headlines into regional risk signals.

## Tests

```bash
cd backend && .venv\Scripts\python -m pytest -q
```

The suite has 16 tests:
- The Monte Carlo properties.
- Auto-execute, escalate→approve, reject, dedupe, and multi-shipment port-congestion flows.
- Receipt → post-mortem → score update.
- The API flow.
- The DeepSeek path, using a stubbed model: parsing, the no-downgrade rule, and invalid-output
  fallback.

## Notes & limits

- **Embeddings are hashed locally.** DeepSeek has no embeddings endpoint, so `rag.embed()` uses a
  dependency-free hashing embedder. For better retrieval, swap in any sentence-embedding model and keep
  `EMBED_DIM` in sync.
- **Supplier quotes are simulated.** `live_quote()` models price drift plus a regional-risk surcharge.
  To use real prices, point it at your supplier pricing API.
- **Schema changes need a reset.** The schema is created with `create_all`. After changing models, add
  Alembic or hit `/api/admin/reset` in dev.
