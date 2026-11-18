"""Central configuration, loaded from environment variables / .env files."""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


def redact_url(url: str) -> str:
    """Hide the password in a connection URL so it is safe to log."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable url>"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- Runtime / security ---
    # "development" keeps local runs frictionless (auth optional, demo seed + admin reset on).
    # "production" refuses to start without API keys and a webhook secret and disables demo features.
    environment: str = "development"
    # Dashboard/approver credentials: "name:key:role" entries, comma separated. Roles: viewer < approver < admin.
    api_keys: str = ""
    # Shared secret for ingestion webhooks. Callers send `X-Signature: sha256=<hmac of raw body>`
    # (or the plain secret in `X-Webhook-Secret` when `webhook_allow_plain_secret` is on).
    webhook_secret: str = ""
    webhook_allow_plain_secret: bool = True
    webhook_max_skew_seconds: int = 300  # when X-Timestamp is sent it must be this fresh (replay protection)
    session_ttl_hours: int = 12  # dashboard login sessions (username + password)
    password_min_length: int = 10
    login_max_failures: int = 8  # failed logins per username per 5 minutes before it is temporarily locked
    seed_demo_data: bool | None = None  # None -> on in development, off in production
    allow_admin_reset: bool | None = None  # None -> on in development, off in production
    rate_limit_per_minute: int = 600  # per client (API key or IP) for API calls; 0 disables
    ingest_rate_limit_per_minute: int = 1200  # per client for ingestion webhooks; 0 disables
    log_format: str = "text"  # text | json
    metrics_enabled: bool = True
    metrics_token: str = ""  # if set, /metrics requires `Authorization: Bearer <token>`

    # --- LLM (DeepSeek, OpenAI-compatible API) ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    llm_timeout_seconds: int = 60
    # Spend guard: once today's (UTC) usage crosses either cap, agents use deterministic fallbacks. 0 = unlimited.
    llm_daily_budget_usd: float = 0.0
    llm_daily_token_budget: int = 0
    llm_price_per_m_input_usd: float = 0.27  # DeepSeek list prices; override if they change
    llm_price_per_m_output_usd: float = 1.10

    # --- Embeddings (RAG) ---
    embedding_provider: str = "hash"  # hash | openai (any OpenAI-compatible /embeddings endpoint)
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embed_dim: int = 384  # must equal the model's output size (OpenAI models accept a `dimensions` param)

    # --- Supplier pricing API (optional; falls back to the built-in price-drift model) ---
    supplier_quote_url: str = ""  # e.g. https://pricing.example.com/quote
    supplier_quote_api_key: str = ""
    supplier_quote_timeout_seconds: float = 5.0

    # --- Storage ---
    # SQLite works out of the box; docker-compose switches to Postgres + TimescaleDB + pgvector.
    database_url: str = "sqlite:///./supplychain.db"
    checkpoint_sqlite_path: str = "./checkpoints.db"
    redis_url: str = ""  # empty -> in-memory event bus
    event_stream: str = "supply.events"
    consumer_group: str = "disruption-agents"

    # --- Guardrails (HITL) ---
    auto_approve_limit: float = 2000.0  # USD; plans above this halt for human approval
    min_auto_supplier_reliability: float = 0.70
    min_auto_llm_confidence: float = 0.5

    # --- Simulation ---
    monte_carlo_runs: int = 10000
    simulation_horizon_days: int = 60

    # --- Worker ---
    run_embedded_worker: bool = True
    background_interval_seconds: int = 30
    enable_external_polling: bool = False  # Open-Meteo weather + NewsAPI polling
    external_poll_interval_seconds: int = 900
    newsapi_key: str = ""
    event_claim_timeout_seconds: int = 600  # a "processing" event older than this is presumed crashed and re-run
    worker_metrics_port: int = 0  # >0: standalone workers expose Prometheus metrics on this port

    # --- Live data feeds (optional pollers) ---
    enable_tracking_polling: bool = False
    tracking_poll_interval_seconds: int = 900
    flightaware_api_key: str = ""  # AeroAPI, used for air shipments (Shipment.tracking_ref = flight ident)
    vessel_tracking_url: str = ""  # ocean tracking URL template, e.g. https://api.example.com/vessels?imo={ref}
    vessel_tracking_api_key: str = ""
    vessel_tracking_key_header: str = "Authorization"
    vessel_tracking_eta_field: str = "eta"  # dotted JSON path to the ETA in the response
    mqtt_url: str = ""  # e.g. mqtt://broker:1883 ; subscribes to telemetry and publishes events
    mqtt_topic: str = "supplychain/telemetry/#"
    mqtt_username: str = ""
    mqtt_password: str = ""

    # --- Integrations (all optional) ---
    slack_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""
    erp_webhook_url: str = ""
    erp_flavor: str = "generic"  # generic | sap | netsuite (selects the payload mapping)
    erp_auth_header: str = "Authorization"  # header carrying the ERP credential
    erp_auth_value: str = ""  # e.g. "Bearer xyz"
    erp_max_attempts: int = 4  # inline attempts (exponential backoff) before queueing for background retry
    erp_retry_max_total_attempts: int = 12

    cors_origins: str = "http://localhost:3000"
    sse_poll_seconds: float = 2.0

    @property
    def llm_enabled(self) -> bool:
        return bool(self.deepseek_api_key.strip())

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        """Hosted providers (Supabase, Neon, ...) hand out plain postgres:// or postgresql:// URLs; this project
        uses the psycopg 3 driver, so rewrite them."""
        v = v.strip()
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_pooled(self) -> bool:
        """True for a transaction-mode pooler (Supabase/PgBouncer on port 6543). Such connections cannot use
        server-side prepared statements or session-level advisory locks."""
        if not self.is_postgres:
            return False
        try:
            return make_url(self.database_url).port == 6543
        except Exception:
            return False

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in ("production", "prod")

    @property
    def seed_enabled(self) -> bool:
        return (not self.is_production) if self.seed_demo_data is None else self.seed_demo_data

    @property
    def admin_reset_enabled(self) -> bool:
        return (not self.is_production) if self.allow_admin_reset is None else self.allow_admin_reset

    @property
    def parsed_api_keys(self) -> dict[str, tuple[str, str]]:
        """key -> (user name, role)."""
        out: dict[str, tuple[str, str]] = {}
        for entry in filter(None, (e.strip() for e in self.api_keys.split(","))):
            parts = entry.split(":")
            if len(parts) < 2:
                raise ValueError("API_KEYS entries must look like name:key[:role]")
            name, key = parts[0], parts[1]
            role = parts[2] if len(parts) > 2 else "approver"
            if role not in ("viewer", "approver", "admin"):
                raise ValueError(f"Unknown role {role!r} for API key {name!r}")
            out[key] = (name, role)
        return out

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys.strip())

    def validate_for_runtime(self) -> list[str]:
        """Return warnings; raise for settings that are unsafe in production."""
        warnings: list[str] = []
        keys = self.parsed_api_keys  # raises on malformed config
        if self.is_production:
            problems = []
            if not self.auth_enabled:
                problems.append("API_KEYS must be set")
            if not self.webhook_secret:
                problems.append("WEBHOOK_SECRET must be set")
            if any(len(k) < 16 for k in keys):
                problems.append("every API key must be at least 16 characters")
            if self.cors_origins.strip() == "*":
                problems.append("CORS_ORIGINS must not be *")
            if problems:
                raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
            if "localhost" in self.cors_origins:
                warnings.append("CORS_ORIGINS still points at localhost; set it to your dashboard origin")
            if not self.is_postgres:
                warnings.append("Production is running on SQLite; use PostgreSQL")
        elif not self.auth_enabled:
            warnings.append("Authentication is DISABLED (no API_KEYS). Fine for local development only.")
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
