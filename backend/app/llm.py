"""DeepSeek LLM access via LangChain's OpenAI-compatible chat client.

Every agent call goes through `structured_call`, which asks DeepSeek for a JSON object matching a
Pydantic schema. If no API key is configured, or the call/validation fails, the caller's deterministic
fallback is used so the pipeline never stalls on the LLM.
"""
import json
import logging
from functools import lru_cache
from typing import Callable, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.config import settings

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


def structured_call(
    system: str,
    user: str | dict,
    schema: type[T],
    fallback: Callable[[], T],
) -> tuple[T, str]:
    """Return (result, source) where source is 'deepseek' or 'fallback'."""
    if not settings.llm_enabled:
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
    try:
        reply = get_chat_model().invoke(messages)
        return schema.model_validate_json(_extract_json(str(reply.content))), "deepseek"
    except ValidationError as exc:
        log.warning("DeepSeek output failed validation for %s: %s", schema.__name__, exc)
    except Exception as exc:
        log.warning("DeepSeek call failed for %s: %s", schema.__name__, exc)
    return fallback(), "fallback"
