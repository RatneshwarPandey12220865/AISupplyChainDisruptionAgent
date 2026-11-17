"""Database engine, session handling and schema bootstrap (PostgreSQL/TimescaleDB/pgvector or SQLite).

The schema is managed with Alembic (``backend/migrations``). ``init_db`` runs ``alembic upgrade head`` on startup
(guarded by a Postgres advisory lock so the API and several workers can boot together).
Create a new migration after changing models with:  ``alembic revision --autogenerate -m "describe change"``.
"""
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
_MIGRATION_LOCK_ID = 727_274_001

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


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["skip_logging"] = True
    return cfg


def _adopt_legacy_schema() -> None:
    """Databases created before Alembic existed (via create_all) have tables but no version row. Add whatever the
    current models need (new tables / nullable-or-defaulted columns), then let Alembic stamp them as current."""
    from app import models  # noqa: F401

    insp = inspect(engine)
    existing = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            table.create(engine)
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'
            default = getattr(col.default, "arg", None) if col.default is not None else None
            if isinstance(default, bool):
                ddl += f" DEFAULT {int(default)}"
            elif isinstance(default, (int, float)):
                ddl += f" DEFAULT {default}"
            elif isinstance(default, str):
                ddl += " DEFAULT '" + default.replace("'", "''") + "'"
            with engine.begin() as conn:
                conn.execute(text(ddl))
            log.warning("Adopted legacy schema: added %s.%s", table.name, col.name)


def run_migrations() -> None:
    from alembic import command

    cfg = _alembic_config()
    tables = set(inspect(engine).get_table_names())
    if tables and "alembic_version" not in tables:
        log.warning("Existing un-versioned database found; adopting it into Alembic")
        _adopt_legacy_schema()
        command.stamp(cfg, "head")
        return
    command.upgrade(cfg, "head")


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    lock_conn = None
    if settings.is_postgres:
        lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        lock_conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _MIGRATION_LOCK_ID})
        for ext in ("vector", "timescaledb"):
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
            except Exception as exc:  # extension not installed on this server
                log.warning("Postgres extension %s unavailable: %s", ext, exc)
    try:
        run_migrations()
        if settings.is_postgres:
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        "SELECT create_hypertable('telemetry_readings', 'ts', if_not_exists => TRUE, "
                        "migrate_data => TRUE)"
                    ))
            except Exception as exc:
                log.warning("TimescaleDB hypertable not created: %s", exc)
    finally:
        if lock_conn is not None:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _MIGRATION_LOCK_ID})
            lock_conn.close()


def drop_everything() -> None:
    """Dev/test helper: drop all tables including the Alembic version table."""
    from app import models  # noqa: F401

    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
