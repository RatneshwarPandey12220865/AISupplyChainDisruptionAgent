"""Central configuration, loaded from environment variables / .env files."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM (DeepSeek, OpenAI-compatible API) ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    llm_timeout_seconds: int = 60

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

    # --- Integrations (all optional) ---
    slack_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""
    erp_webhook_url: str = ""

    cors_origins: str = "http://localhost:3000"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.deepseek_api_key.strip())

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
