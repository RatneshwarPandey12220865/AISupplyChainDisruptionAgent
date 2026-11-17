"""Supplier pricing API client.

When SUPPLIER_QUOTE_URL is set the sourcing agent asks it for a live quote per supplier/SKU:

    POST {SUPPLIER_QUOTE_URL}
    Authorization: Bearer {SUPPLIER_QUOTE_API_KEY}
    {"supplier_id": 3, "supplier": "Bavaria Components GmbH", "sku": "PSU-65W", "region": "Europe"}
    -> {"unit_price": 11.4, "lead_time_days": 12, "currency": "USD"}   (currency optional, USD assumed)

Responses are cached for QUOTE_CACHE_SECONDS. Any failure returns ``None`` so the caller falls back to the
built-in price-drift model and the pipeline keeps working.
"""
import logging
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)
QUOTE_CACHE_SECONDS = 600
_cache: dict[tuple[int, str], tuple[float, dict]] = {}


def remote_quote(supplier_id: int, supplier_name: str, region: str, sku: str) -> dict | None:
    if not settings.supplier_quote_url:
        return None
    key = (supplier_id, sku)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < QUOTE_CACHE_SECONDS:
        return hit[1]
    try:
        headers = {"Authorization": f"Bearer {settings.supplier_quote_api_key}"} if settings.supplier_quote_api_key else {}
        resp = httpx.post(settings.supplier_quote_url, headers=headers, timeout=settings.supplier_quote_timeout_seconds,
                          json={"supplier_id": supplier_id, "supplier": supplier_name, "sku": sku, "region": region})
        resp.raise_for_status()
        body = resp.json()
        quote = {"unit_price": float(body["unit_price"]), "lead_time_days": float(body["lead_time_days"])}
        if quote["unit_price"] <= 0 or quote["lead_time_days"] <= 0:
            raise ValueError("non-positive quote")
    except Exception as exc:
        log.warning("Supplier quote API failed for %s/%s: %s (using model)", supplier_name, sku, exc)
        return None
    _cache[key] = (time.monotonic(), quote)
    return quote
