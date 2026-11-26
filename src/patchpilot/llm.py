"""One structured-output request to a model, with caching, validation and logging."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import anthropic
import httpx2
from pydantic import BaseModel, ValidationError

from patchpilot.cache import ResponseCache
from patchpilot.config import Settings
from patchpilot.cost import cost_usd
from patchpilot.providers import LLMError, Prompt, Reply, create_backend
from patchpilot.store import RequestRecord

__all__ = ["LLMClient", "LLMError", "Recorder"]

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
Recorder = Callable[[RequestRecord], None]


class LLMClient:
    """Sends prompts to a model and returns replies validated against a pydantic model."""

    def __init__(
        self,
        settings: Settings,
        on_request: Recorder,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self._settings = settings
        self._backend = create_backend(settings, http_client)
        # A callback, so the CLI can write to SQLite and the eval can collect in memory.
        self._on_request = on_request
        self._cache = ResponseCache(settings.cache_dir) if settings.response_cache else None

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[SchemaT],
        feature: str,
        model: str | None = None,
        tokens_saved: int | None = None,
    ) -> SchemaT:
        """Return the reply as an instance of `schema`, from the local cache or the API."""
        if len(user) > self._settings.max_prompt_chars:
            raise LLMError(
                f"prompt has {len(user)} characters, over the limit of "
                f"{self._settings.max_prompt_chars} (PATCHPILOT_MAX_PROMPT_CHARS)"
            )
        model = model or self._settings.model
        # A plain function that makes a pydantic schema strict; it sends nothing to Anthropic.
        prompt = Prompt(model, system, user, anthropic.transform_schema(schema))
        # The key covers everything that shapes a reply, so a changed diff, prompt or model
        # can never be answered with an old result.
        key = ResponseCache.key(
            {
                "provider": self._settings.provider,
                "model": model,
                "effort": self._settings.effort,
                "max_tokens": self._settings.max_tokens,
                "system": system,
                "user": user,
                "schema": prompt.schema,
            }
        )
        started = time.perf_counter()
        cached = self._read_cache(key, schema)
        if cached is not None:
            latency = time.perf_counter() - started
            self._on_request(
                _record_without_usage(
                    "local-cache", feature, model, "cache_hit", 0.0, latency, tokens_saved
                )
            )
            return cached

        # Source code reaches the log only when --debug attaches a handler.
        logger.debug("request to %s\nsystem:\n%s\nuser:\n%s", model, system, user)
        self._backend.wait_for_turn()
        started = time.perf_counter()
        try:
            reply = self._backend.send(prompt)
        except LLMError as error:
            # No reply means no token counts, so the cost is unknown.
            latency = time.perf_counter() - started
            self._on_request(
                _record_without_usage(
                    error.request_id or "unknown",
                    feature,
                    model,
                    error.status,
                    None,
                    latency,
                    tokens_saved,
                )
            )
            raise
        latency = time.perf_counter() - started

        # Log before raising: a refused or cut-off reply still costs money.
        try:
            result = _read_reply(reply, schema)
        except LLMError as error:
            self._on_request(self._record(reply, feature, error.status, latency, tokens_saved))
            raise
        self._on_request(self._record(reply, feature, "ok", latency, tokens_saved))
        if self._cache is not None:
            self._cache.put(key, result.model_dump_json())
        return result

    def _read_cache(self, key: str, schema: type[SchemaT]) -> SchemaT | None:
        if self._cache is None:
            return None
        text = self._cache.get(key)
        if text is None:
            return None
        try:
            return schema.model_validate_json(text)
        except ValidationError:
            logger.warning("ignoring a damaged response cache entry")
            return None

    def _record(
        self,
        reply: Reply,
        feature: str,
        status: str,
        latency: float,
        tokens_saved: int | None,
    ) -> RequestRecord:
        usages = [usage for _, usage in reply.attempts]
        costs = [cost_usd(model, usage, self._settings.prices) for model, usage in reply.attempts]
        known = [cost for cost in costs if cost is not None]
        return RequestRecord(
            request_id=reply.request_id,
            feature=feature,
            model=reply.model,
            status=status,
            input_tokens=sum(usage.input_tokens for usage in usages),
            output_tokens=sum(usage.output_tokens for usage in usages),
            cache_write_tokens=sum(usage.cache_write_tokens for usage in usages),
            cache_read_tokens=sum(usage.cache_read_tokens for usage in usages),
            # No token counts, or one unpriced attempt, makes the total unknown.
            cost_usd=sum(known) if costs and len(known) == len(costs) else None,
            latency_seconds=latency,
            tokens_saved=tokens_saved,
        )


def _record_without_usage(
    request_id: str,
    feature: str,
    model: str,
    status: str,
    cost: float | None,
    latency: float,
    tokens_saved: int | None,
) -> RequestRecord:
    """A log entry for a call that returned no token counts."""
    return RequestRecord(
        request_id=request_id,
        feature=feature,
        model=model,
        status=status,
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
        cost_usd=cost,
        latency_seconds=latency,
        tokens_saved=tokens_saved,
    )


def _read_reply(reply: Reply, schema: type[SchemaT]) -> SchemaT:
    logger.debug("reply (status=%s):\n%s", reply.status, reply.text)
    if reply.status == "refusal":
        raise LLMError("the model declined to answer this request", status="refusal")
    if reply.status == "max_tokens":
        raise LLMError("the reply was cut off; raise PATCHPILOT_MAX_TOKENS", status="max_tokens")
    try:
        return schema.model_validate_json(reply.text)
    except ValidationError as error:
        raise LLMError(
            f"the reply did not match the expected JSON structure "
            f"({error.error_count()} validation errors)",
            status="invalid_reply",
        ) from error
