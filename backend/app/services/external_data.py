"""External environment feeds that raise risk signals before suppliers report delays.

* Weather: Open-Meteo forecast API (free, no key) polled at each supplier's coordinates.
* News / geopolitics: NewsAPI (NEWSAPI_KEY) headlines classified by DeepSeek into region + severity.
* Maritime / air tracking (VesselFinder, FlightAware, ...): these push position/ETA updates to
  POST /api/ingest/telemetry (metric=eta_update) through their webhook products, so no poller is needed.
"""
import logging

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm import structured_call
from app.models import Supplier

log = logging.getLogger(__name__)

WIND_ALERT_KMH = 70
RAIN_ALERT_MM = 80


def poll_weather(session: Session) -> list[dict]:
    signals = []
    suppliers = session.scalars(select(Supplier).where(Supplier.is_active)).all()
    for s in suppliers:
        try:
            resp = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
                "latitude": s.lat, "longitude": s.lon, "forecast_days": 3,
                "daily": "wind_speed_10m_max,precipitation_sum",
            })
            resp.raise_for_status()
            daily = resp.json()["daily"]
        except Exception as exc:
            log.warning("Open-Meteo failed for %s: %s", s.name, exc)
            continue
        wind = max(daily.get("wind_speed_10m_max") or [0])
        rain = max(daily.get("precipitation_sum") or [0])
        if wind >= WIND_ALERT_KMH or rain >= RAIN_ALERT_MM:
            severity = min(1.0, max(wind / 150, rain / 200))
            signals.append({"kind": "weather", "region": s.region, "severity": round(severity, 2),
                            "source": "open-meteo",
                            "description": f"Severe weather forecast near {s.port or s.name}: "
                                           f"wind up to {wind:.0f} km/h, rain {rain:.0f} mm in 3 days"})
    return signals


class NewsRisk(BaseModel):
    relevant: bool
    kind: str = Field(description="port_congestion | weather | strike | geopolitical")
    region: str = Field(description="One of the supplier regions provided, or 'none'")
    severity: float = Field(ge=0, le=1)
    description: str


def poll_news(session: Session) -> list[dict]:
    if not settings.newsapi_key:
        return []
    regions = sorted({s.region for s in session.scalars(select(Supplier)).all()})
    try:
        resp = httpx.get("https://newsapi.org/v2/everything", timeout=10, params={
            "q": '(port OR shipping OR freight) AND (strike OR congestion OR typhoon OR closure OR blockade)',
            "sortBy": "publishedAt", "pageSize": 10, "language": "en", "apiKey": settings.newsapi_key,
        })
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
    except Exception as exc:
        log.warning("NewsAPI failed: %s", exc)
        return []

    signals = []
    for a in articles:
        headline = f"{a.get('title', '')}. {a.get('description') or ''}"
        risk, source = structured_call(
            "You are a supply-chain risk analyst. Decide whether a news item threatens shipping or supplier "
            "operations in one of the given regions and rate severity 0-1.",
            {"headline": headline, "supplier_regions": regions},
            NewsRisk,
            fallback=lambda: NewsRisk(relevant=False, kind="geopolitical", region="none", severity=0, description=""),
        )
        if source == "deepseek" and risk.relevant and risk.region in regions and risk.severity >= 0.4:
            signals.append({"kind": risk.kind, "region": risk.region, "severity": risk.severity,
                            "description": risk.description, "source": "newsapi"})
    return signals
