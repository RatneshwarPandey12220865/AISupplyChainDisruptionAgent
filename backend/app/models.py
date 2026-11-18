"""Relational schema: suppliers, inventory, orders, disruptions, telemetry (hypertable) and contracts (vectors)."""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db import Base

EMBED_DIM = settings.embed_dim

if settings.is_postgres:
    from pgvector.sqlalchemy import Vector

    EmbeddingType: Any = Vector(EMBED_DIM)
else:
    EmbeddingType = JSON


def utcnow() -> datetime:
    """Naive UTC timestamp (portable across SQLite and Postgres)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    """Dashboard account (username + password). Created by an admin; the bootstrap admin comes from API_KEYS."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class UserSession(Base):
    """Server-side login session. Only a SHA-256 of the token is stored, so a database leak cannot be replayed."""
    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    country: Mapped[str] = mapped_column(String(80))
    region: Mapped[str] = mapped_column(String(80), index=True)
    port: Mapped[str] = mapped_column(String(120), default="")
    lat: Mapped[float] = mapped_column(Float, default=0.0)
    lon: Mapped[float] = mapped_column(Float, default=0.0)
    tier: Mapped[int] = mapped_column(Integer, default=1)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.85)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(40), default="operational")
    contact_email: Mapped[str] = mapped_column(String(200), default="")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    unit_cost: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    daily_demand_mean: Mapped[float] = mapped_column(Float)
    daily_demand_std: Mapped[float] = mapped_column(Float)
    reorder_point: Mapped[float] = mapped_column(Float)
    safety_stock: Mapped[float] = mapped_column(Float)
    stockout_penalty_per_unit: Mapped[float] = mapped_column(Float, default=0.0)
    primary_supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"))
    primary_supplier: Mapped[Optional[Supplier]] = relationship()


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))


class InventoryLevel(Base):
    __tablename__ = "inventory_levels"
    __table_args__ = (UniqueConstraint("product_id", "warehouse_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    on_hand: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship()


class SupplierProduct(Base):
    """Which supplier can deliver which product, at what price and lead time."""
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("supplier_id", "product_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    unit_price: Mapped[float] = mapped_column(Float)
    lead_time_days: Mapped[float] = mapped_column(Float)
    lead_time_std_days: Mapped[float] = mapped_column(Float, default=1.0)
    capacity_per_week: Mapped[float] = mapped_column(Float)
    min_order_qty: Mapped[float] = mapped_column(Float, default=0)
    expedite_available: Mapped[bool] = mapped_column(Boolean, default=False)
    expedite_fee_pct: Mapped[float] = mapped_column(Float, default=0.15)
    supplier: Mapped[Supplier] = relationship()
    product: Mapped[Product] = relationship()


class SupplierDelivery(Base):
    """Historical delivery performance, feeds lead-time statistics and reliability scoring."""
    __tablename__ = "supplier_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    promised_days: Mapped[float] = mapped_column(Float)
    actual_days: Mapped[float] = mapped_column(Float)
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Shipment(Base):
    """Inbound shipment in transit from a supplier to a warehouse."""
    __tablename__ = "shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    quantity: Mapped[float] = mapped_column(Float)
    original_eta: Mapped[datetime] = mapped_column(DateTime)
    eta: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), default="in_transit")
    mode: Mapped[str] = mapped_column(String(20), default="ocean")
    carrier: Mapped[str] = mapped_column(String(80), default="")
    vessel: Mapped[str] = mapped_column(String(80), default="")
    route_region: Mapped[str] = mapped_column(String(80), default="")
    # Carrier-side identifier used by the live trackers: IMO/MMSI for ocean, flight ident for air.
    tracking_ref: Mapped[str] = mapped_column(String(80), default="")
    supplier: Mapped[Supplier] = relationship()
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship()


class CustomerOrder(Base):
    __tablename__ = "customer_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(40), unique=True)
    customer: Mapped[str] = mapped_column(String(120))
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    due_date: Mapped[datetime] = mapped_column(DateTime)
    penalty_per_day_late: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(30), default="open")


class PurchaseOrder(Base):
    """ERP write-back record: new POs, expedite requests and inter-warehouse transfers."""
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # purchase | expedite | transfer
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    warehouse_id: Mapped[Optional[int]] = mapped_column(ForeignKey("warehouses.id"))
    source_warehouse_id: Mapped[Optional[int]] = mapped_column(ForeignKey("warehouses.id"))
    quantity: Mapped[float] = mapped_column(Float)
    unit_price: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[float] = mapped_column(Float)
    expected_days: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="submitted")  # submitted | received | cancelled
    disruption_id: Mapped[Optional[int]] = mapped_column(ForeignKey("disruptions.id"), index=True)
    erp_ref: Mapped[str] = mapped_column(String(80), default="")
    # ERP sync outbox: not_configured | pending | synced | failed (pending rows are retried in the background)
    erp_status: Mapped[str] = mapped_column(String(20), default="not_configured", index=True)
    erp_attempts: Mapped[int] = mapped_column(Integer, default=0)
    erp_last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    actual_days: Mapped[Optional[float]] = mapped_column(Float)
    supplier: Mapped[Optional[Supplier]] = relationship()
    product: Mapped[Product] = relationship()
    warehouse: Mapped[Optional[Warehouse]] = relationship(foreign_keys=[warehouse_id])


class Disruption(Base):
    """One detected disruption and everything the agent graph produced for it."""
    __tablename__ = "disruptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    event_id: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(30), default="detected", index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    supplier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("suppliers.id"))
    warehouse_id: Mapped[Optional[int]] = mapped_column(ForeignKey("warehouses.id"))
    shipment_id: Mapped[Optional[int]] = mapped_column(ForeignKey("shipments.id"))
    anomaly: Mapped[dict] = mapped_column(JSON, default=dict)
    triage: Mapped[dict] = mapped_column(JSON, default=dict)
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    simulation: Mapped[dict] = mapped_column(JSON, default=dict)
    options: Mapped[list] = mapped_column(JSON, default=list)
    recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    guardrail: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    execution: Mapped[dict] = mapped_column(JSON, default=dict)
    postmortem: Mapped[dict] = mapped_column(JSON, default=dict)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    expected_recovery_days: Mapped[Optional[float]] = mapped_column(Float)
    actual_recovery_days: Mapped[Optional[float]] = mapped_column(Float)
    value_at_risk: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    product: Mapped[Optional[Product]] = relationship()
    supplier: Mapped[Optional[Supplier]] = relationship()
    warehouse: Mapped[Optional[Warehouse]] = relationship()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(20), default="info")
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    channels: Mapped[list] = mapped_column(JSON, default=list)
    disruption_id: Mapped[Optional[int]] = mapped_column(ForeignKey("disruptions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    source: Mapped[str] = mapped_column(String(60), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | processing | processed | failed
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)  # set when a worker starts processing


class TelemetryReading(Base):
    """IoT / tracking time series. Becomes a TimescaleDB hypertable on Postgres."""
    __tablename__ = "telemetry_readings"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True, default=utcnow)
    shipment_code: Mapped[str] = mapped_column(String(40), index=True, default="")
    metric: Mapped[str] = mapped_column(String(40))
    value: Mapped[float] = mapped_column(Float)
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lon: Mapped[Optional[float]] = mapped_column(Float)


class ExternalSignal(Base):
    """Weather, port congestion, geopolitical and labour events affecting a region."""
    __tablename__ = "external_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))  # port_congestion | weather | strike | geopolitical
    region: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[float] = mapped_column(Float)  # 0..1
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(80), default="manual")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class SupplierContract(Base):
    """Contract / SLA / compliance chunks, vectorised for retrieval (pgvector on Postgres)."""
    __tablename__ = "supplier_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(EmbeddingType)
    supplier: Mapped[Supplier] = relationship()


class SupplierScoreHistory(Base):
    __tablename__ = "supplier_score_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    old_score: Mapped[float] = mapped_column(Float)
    new_score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, default="")
    disruption_id: Mapped[Optional[int]] = mapped_column(ForeignKey("disruptions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LLMUsage(Base):
    """One row per LLM call: feeds the daily budget guard and cost reporting."""
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    model: Mapped[str] = mapped_column(String(80), default="")
    schema_name: Mapped[str] = mapped_column(String(80), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
