"""Database engine, session handling and schema bootstrap (PostgreSQL/TimescaleDB/pgvector or SQLite)."""
import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

log = logging.getLogger(__name__)

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    if settings.is_postgres:
        for ext in ("vector", "timescaledb"):
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
            except Exception as exc:  # extension not installed on this server
                log.warning("Postgres extension %s unavailable: %s", ext, exc)
    Base.metadata.create_all(engine)
    if settings.is_postgres:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "SELECT create_hypertable('telemetry_readings', 'ts', if_not_exists => TRUE, migrate_data => TRUE)"
                ))
        except Exception as exc:
            log.warning("TimescaleDB hypertable not created: %s", exc)
