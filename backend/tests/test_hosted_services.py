"""Supabase / managed Redis support: URL normalisation, pooler detection, secret redaction."""
import pytest

from app.config import Settings, redact_url


@pytest.mark.parametrize("raw", [
    "postgresql://postgres.abc:pw@aws-0-eu.pooler.supabase.com:5432/postgres",
    "postgres://postgres.abc:pw@aws-0-eu.pooler.supabase.com:5432/postgres",
])
def test_supabase_urls_use_psycopg3(raw):
    s = Settings(database_url=raw)
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.is_postgres and not s.is_pooled


def test_transaction_pooler_detected():
    s = Settings(database_url="postgresql://postgres.abc:pw@aws-0-eu.pooler.supabase.com:6543/postgres")
    assert s.is_pooled


def test_explicit_driver_and_sqlite_untouched():
    assert Settings(database_url="postgresql+psycopg://u:p@h/db").database_url == "postgresql+psycopg://u:p@h/db"
    s = Settings(database_url="sqlite:///./x.db")
    assert s.database_url == "sqlite:///./x.db" and not s.is_pooled


def test_redact_url_hides_password():
    out = redact_url("rediss://default:s3cr3t-token@eu1-x.upstash.io:6379")
    assert "s3cr3t-token" not in out and "upstash.io" in out
