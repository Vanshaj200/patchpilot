"""Claude API wrapper: one structured-output request with caching, retries and validation."""

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage
from pydantic import BaseModel, ValidationError

from patchpilot.cache import ResponseCache, text_block
from patchpilot.config import Settings
from patchpilot.cost import TokenUsage, cost_usd
from patchpilot.store import RequestRecord

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
Recorder = Callable[[RequestRecord], None]

# These models accept server-side refusal fallbacks: if a safety check declines a request
# (possible when reviewing security-related code), the API reruns it on another model.
_FALLBACK_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-fable-5-2",
        "claude-fable-5-1",
    }
)
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """A request failed or returned something unusable. `status` is stored in the usage log."""

    def __init__(self, message: str, status: str = "error") -> None:
        super().__init__(message)
        self.status = status


class LLMClient:
    """Sends prompts to Claude and returns replies validated against a pydantic model."""

    def __init__(
        self,
        settings: Settings,
        on_request: Recorder,
        http_client: httpx2.Client | None = None,
    ) -> None:
        if settings.api_key is None or not settings.api_key.get_secret_value():
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._settings = settings
        # A callback, so the CLI can write to SQLite and the eval can collect in memory.
        self._on_request = on_request
        self._cache = ResponseCache(settings.cache_dir) if settings.response_cache else None
        # The SDK retries connection errors, timeouts, rate limits and server errors with
        # backoff. A second retry loop here would multiply the number of attempts.
        self._client = anthropic.Anthropic(
            api_key=settings.api_key.get_secret_value(),
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

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
        json_schema = anthropic.transform_schema(schema)
        # The key covers everything that shapes a reply, so a changed diff, prompt or model
        # can never be answered with an old result.
        key = ResponseCache.key(
            {
                "model": model,
                "effort": self._settings.effort,
                "max_tokens": self._settings.max_tokens,
                "system": system,
                "user": user,
                "schema": json_schema,
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

        use_prompt_cache = self._settings.prompt_cache
        cache_user = use_prompt_cache and self._settings.cache_file_context
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": self._settings.max_tokens,
            "system": [text_block(system, use_prompt_cache)],
            "messages": [{"role": "user", "content": [text_block(user, cache_user)]}],
            "output_config": {
                "effort": self._settings.effort,
                "format": {"type": "json_schema", "schema": json_schema},
            },
        }
        if model in _FALLBACK_MODELS:
            request |= {"betas": [_FALLBACK_BETA], "fallbacks": "default"}

        # Source code reaches the log only when --debug attaches a handler.
        logger.debug("request to %s\nsystem:\n%s\nuser:\n%s", model, system, user)
        started = time.perf_counter()
        try:
            response = self._client.beta.messages.create(**request)
        except anthropic.APIError as error:
            # No reply means no token counts, so the cost is unknown.
            request_id = getattr(error, "request_id", None) or "unknown"
            latency = time.perf_counter() - started
            self._on_request(
                _record_without_usage(
                    request_id, feature, model, "api_error", None, latency, tokens_saved
                )
            )
            raise LLMError(f"Claude API request failed: {error.message}") from error
        latency = time.perf_counter() - started

        # Log before raising: a refused or cut-off reply still costs money.
        try:
            result = _read_reply(response, schema)
        except LLMError as error:
            self._on_request(self._record(response, feature, error.status, latency, tokens_saved))
            raise
        self._on_request(self._record(response, feature, "ok", latency, tokens_saved))
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
        response: BetaMessage,
        feature: str,
        status: str,
        latency: float,
        tokens_saved: int | None,
    ) -> RequestRecord:
        attempts = _attempts(response)
        costs = [cost_usd(model, usage, self._settings.prices) for model, usage in attempts]
        known = [cost for cost in costs if cost is not None]
        return RequestRecord(
            request_id=response._request_id or response.id,
            feature=feature,
            model=response.model,
            status=status,
            input_tokens=sum(usage.input_tokens for _, usage in attempts),
            output_tokens=sum(usage.output_tokens for _, usage in attempts),
            cache_write_tokens=sum(usage.cache_write_tokens for _, usage in attempts),
            cache_read_tokens=sum(usage.cache_read_tokens for _, usage in attempts),
            # One unpriced attempt makes the total unknown.
            cost_usd=sum(known) if len(known) == len(costs) else None,
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


def _attempts(response: BetaMessage) -> list[tuple[str, TokenUsage]]:
    """List every model attempt behind a reply with its token counts.

    After a refusal fallback, `usage.iterations` has one entry per model that ran, while the
    top-level usage covers only the last one.
    """
    entries = response.usage.iterations or [response.usage]
    return [
        (
            getattr(entry, "model", None) or response.model,
            TokenUsage(
                input_tokens=entry.input_tokens,
                output_tokens=entry.output_tokens,
                cache_write_tokens=entry.cache_creation_input_tokens or 0,
                cache_read_tokens=entry.cache_read_input_tokens or 0,
            ),
        )
        for entry in entries
    ]


def _read_reply(response: BetaMessage, schema: type[SchemaT]) -> SchemaT:
    text = "".join(block.text for block in response.content if block.type == "text")
    logger.debug("response (stop_reason=%s):\n%s", response.stop_reason, text)
    if response.stop_reason == "refusal":
        raise LLMError("Claude declined to answer this request", status="refusal")
    if response.stop_reason == "max_tokens":
        raise LLMError("the reply was cut off; raise PATCHPILOT_MAX_TOKENS", status="max_tokens")
    # Validate here, not with messages.parse(), so the response is kept if validation fails.
    try:
        return schema.model_validate_json(text)
    except ValidationError as error:
        raise LLMError(
            f"the reply did not match the expected JSON structure "
            f"({error.error_count()} validation errors)",
            status="invalid_reply",
        ) from error
