"""DeepSeek LLM access via LangChain's OpenAI-compatible chat client.

Every agent call goes through `structured_call`, which asks DeepSeek for a JSON object matching a
Pydantic schema. If no API key is configured, the daily budget is exhausted, or the call/validation
fails, the caller's deterministic fallback is used so the pipeline never stalls on (or overspends on) the LLM.

Each call is metered (tokens, estimated cost, latency) into the `llm_usage` table and Prometheus.
"""
import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from app.config import settings
from app.metrics import LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


@lru_cache
def get_chat_model():
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_seconds,
        max_retries=2,
    ).bind(response_format={"type": "json_object"})


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end != -1 else text


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * settings.llm_price_per_m_input_usd
            + output_tokens * settings.llm_price_per_m_output_usd) / 1_000_000


def _today_start() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def budget_exhausted() -> bool:
    """True once today's recorded spend/tokens reach the configured caps (0 = unlimited)."""
    if not (settings.llm_daily_budget_usd or settings.llm_daily_token_budget):
        return False
    from app.db import session_scope
    from app.models import LLMUsage

    try:
        with session_scope() as s:
            cost, tokens = s.execute(
                select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
                       func.coalesce(func.sum(LLMUsage.input_tokens + LLMUsage.output_tokens), 0))
                .where(LLMUsage.created_at >= _today_start())).one()
    except Exception as exc:  # never let accounting take the pipeline down
        log.warning("LLM budget check failed (%s); allowing call", exc)
        return False
    return bool((settings.llm_daily_budget_usd and cost >= settings.llm_daily_budget_usd)
                or (settings.llm_daily_token_budget and tokens >= settings.llm_daily_token_budget))


def _record_usage(schema_name: str, reply, latency: float, ok: bool) -> None:
    usage = getattr(reply, "usage_metadata", None) or {}
    tin, tout = int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)
    cost = estimate_cost(tin, tout)
    LLM_TOKENS.labels("input").inc(tin)
    LLM_TOKENS.labels("output").inc(tout)
    LLM_COST.inc(cost)
    LLM_LATENCY.labels(schema_name).observe(latency)
    try:
        from app.db import session_scope
        from app.models import LLMUsage

        with session_scope() as s:
            s.add(LLMUsage(model=settings.deepseek_model, schema_name=schema_name, input_tokens=tin,
                           output_tokens=tout, cost_usd=cost, latency_ms=latency * 1000, ok=ok))
    except Exception as exc:
        log.warning("Could not record LLM usage: %s", exc)


def structured_call(
    system: str,
    user: str | dict,
    schema: type[T],
    fallback: Callable[[], T],
) -> tuple[T, str]:
    """Return (result, source) where source is 'deepseek' or 'fallback'."""
    name = schema.__name__
    if not settings.llm_enabled:
        LLM_CALLS.labels(name, "disabled").inc()
        return fallback(), "fallback"
    if budget_exhausted():
        log.warning("LLM daily budget exhausted; using deterministic fallback for %s", name)
        LLM_CALLS.labels(name, "budget").inc()
        return fallback(), "fallback"

    user_text = user if isinstance(user, str) else json.dumps(user, default=str, indent=1)
    schema_text = json.dumps(schema.model_json_schema(), indent=1)
    messages = [
        SystemMessage(content=(
            f"{system}\n\nRespond ONLY with a single json object that validates against this JSON schema:\n"
            f"{schema_text}"
        )),
        HumanMessage(content=user_text),
    ]
    started = time.monotonic()
    try:
        reply = get_chat_model().invoke(messages)
    except Exception as exc:
        log.warning("DeepSeek call failed for %s: %s", name, exc)
        LLM_CALLS.labels(name, "fallback").inc()
        return fallback(), "fallback"
    latency = time.monotonic() - started
    try:
        result = schema.model_validate_json(_extract_json(str(reply.content)))
    except ValidationError as exc:
        log.warning("DeepSeek output failed validation for %s: %s", name, exc)
        _record_usage(name, reply, latency, ok=False)
        LLM_CALLS.labels(name, "fallback").inc()
        return fallback(), "fallback"
    _record_usage(name, reply, latency, ok=True)
    LLM_CALLS.labels(name, "ok").inc()
    return result, "deepseek"
