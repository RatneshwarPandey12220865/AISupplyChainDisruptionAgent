"""Request bodies for the ingestion and action endpoints."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class InventoryUpdate(BaseModel):
    sku: str
    warehouse: str
    on_hand: float = Field(ge=0)


class SupplierUpdate(BaseModel):
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    sku: Optional[str] = None
    lead_time_days: Optional[float] = Field(default=None, gt=0)
    status: Optional[Literal["operational", "capacity_reduced", "shutdown", "force_majeure"]] = None
    message: str = ""


class ShipmentEtaUpdate(BaseModel):
    shipment_code: str
    new_eta: Optional[datetime] = None
    delay_days: Optional[float] = None
    reason: str = ""


class TelemetryReading(BaseModel):
    shipment_code: str = ""
    metric: str = Field(description="e.g. temperature_c, shock_g, humidity_pct")
    value: float
    lat: Optional[float] = None
    lon: Optional[float] = None
    ts: Optional[datetime] = None


class ExternalSignalIn(BaseModel):
    kind: Literal["port_congestion", "weather", "strike", "geopolitical"]
    region: str
    severity: float = Field(ge=0, le=1)
    description: str = ""
    duration_hours: float = 72


class OrderCreated(BaseModel):
    order_number: str
    customer: str
    is_vip: bool = False
    sku: str
    warehouse: str
    quantity: float = Field(gt=0)
    unit_price: Optional[float] = None
    due_date: datetime
    penalty_per_day_late: float = 0


class Decision(BaseModel):
    approved: bool
    option_id: Optional[str] = Field(default=None, description="Override the recommended option")
    reviewer: str = Field(default="", description="Ignored when auth is enabled: the API key owner is recorded")
    comment: str = ""


class Resolve(BaseModel):
    actual_recovery_days: Optional[float] = None
    actual_loss: Optional[float] = None


class Receive(BaseModel):
    actual_days: Optional[float] = Field(default=None, ge=0)
