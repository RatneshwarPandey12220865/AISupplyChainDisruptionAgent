"""Prometheus metrics. The API serves them at /metrics; standalone workers can expose their own port
(WORKER_METRICS_PORT) because agent/LLM metrics are produced in the worker process."""
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["method", "route"],
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
RATE_LIMITED = Counter("rate_limited_total", "Requests rejected by the rate limiter", ["scope"])
AUTH_FAILURES = Counter("auth_failures_total", "Failed authentication attempts", ["kind"])

EVENTS = Counter("events_total", "Events handled by the agent worker", ["type", "status"])
AGENT_RUN_SECONDS = Histogram("agent_graph_duration_seconds", "LangGraph run time per anomaly",
                              buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300))
DISRUPTIONS = Counter("disruptions_total", "Agent runs by outcome", ["status"])

LLM_CALLS = Counter("llm_calls_total", "LLM calls", ["schema", "outcome"])  # ok|fallback|budget|disabled
LLM_TOKENS = Counter("llm_tokens_total", "LLM tokens", ["direction"])
LLM_COST = Counter("llm_cost_usd_total", "Estimated LLM spend in USD")
LLM_LATENCY = Histogram("llm_call_duration_seconds", "LLM latency", ["schema"],
                        buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 120))

ERP_SYNC = Counter("erp_sync_total", "ERP write-back attempts", ["result"])  # ok|retry|failed
FEED_POLLS = Counter("feed_polls_total", "Live data feed polls", ["feed", "result"])
SSE_CLIENTS = Gauge("sse_clients", "Connected dashboard live-update streams")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
