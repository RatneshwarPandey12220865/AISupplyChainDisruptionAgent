"""Helpers shared by agent nodes."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Disruption, ExternalSignal, SupplierDelivery, utcnow


def record(session: Session, disruption_id: int, node: str, message: str, **fields) -> Disruption:
    """Persist node output onto the disruption row and append a trace entry (drives the UI timeline)."""
    d = session.get(Disruption, disruption_id)
    for key, value in fields.items():
        setattr(d, key, value)
    d.trace = [*(d.trace or []), {"node": node, "message": message, "at": utcnow().isoformat()}]
    return d


def days_until(dt: datetime) -> float:
    return max(0.0, (dt - utcnow()).total_seconds() / 86400)


def active_signals(session: Session, region: str) -> list[ExternalSignal]:
    now = utcnow()
    return list(session.scalars(select(ExternalSignal).where(
        ExternalSignal.region == region, ExternalSignal.active,
        (ExternalSignal.expires_at.is_(None)) | (ExternalSignal.expires_at > now))).all())


def regional_risk(session: Session, region: str) -> float:
    """Combined 0..1 risk of independent active signals in a region: 1 - prod(1 - severity)."""
    risk = 1.0
    for s in active_signals(session, region):
        risk *= 1 - s.severity
    return round(1 - risk, 3)


def delay_stats(session: Session, supplier_id: int, product_id: int | None = None) -> tuple[float, float]:
    """Historical mean and std of (actual - promised) delivery days for a supplier."""
    stmt = select(SupplierDelivery.actual_days - SupplierDelivery.promised_days).where(
        SupplierDelivery.supplier_id == supplier_id)
    if product_id:
        stmt = stmt.where(SupplierDelivery.product_id == product_id)
    rows = [float(r) for r in session.scalars(stmt).all()]
    if not rows:
        return 0.0, 1.5
    mean = sum(rows) / len(rows)
    std = (sum((r - mean) ** 2 for r in rows) / max(1, len(rows) - 1)) ** 0.5
    return mean, max(0.5, std)
