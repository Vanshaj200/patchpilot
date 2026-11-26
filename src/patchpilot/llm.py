"""Claude API wrapper: one structured-output request with a timeout, retries and validation."""

import logging
from typing import Any, TypeVar

import anthropic
import httpx2
from pydantic import BaseModel, ValidationError

from patchpilot.config import Settings

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

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
    """A request failed or returned something unusable."""


class LLMClient:
    """Sends prompts to Claude and returns replies validated against a pydantic model."""

    def __init__(self, settings: Settings, http_client: httpx2.Client | None = None) -> None:
        if settings.api_key is None or not settings.api_key.get_secret_value():
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._settings = settings
        # The SDK retries connection errors, timeouts, rate limits and server errors with
        # backoff. A second retry loop here would multiply the number of attempts.
        self._client = anthropic.Anthropic(
            api_key=settings.api_key.get_secret_value(),
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    def complete(self, *, system: str, user: str, schema: type[SchemaT]) -> SchemaT:
        """Send one request and return the reply as an instance of `schema`."""
        if len(user) > self._settings.max_prompt_chars:
            raise LLMError(
                f"prompt has {len(user)} characters, over the limit of "
                f"{self._settings.max_prompt_chars} (PATCHPILOT_MAX_PROMPT_CHARS)"
            )
        model = self._settings.model
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": self._settings.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self._settings.effort,
                "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
            },
        }
        if model in _FALLBACK_MODELS:
            request |= {"betas": [_FALLBACK_BETA], "fallbacks": "default"}

        # Source code reaches the log only when --debug attaches a handler.
        logger.debug("request to %s\nsystem:\n%s\nuser:\n%s", model, system, user)
        try:
            response = self._client.beta.messages.create(**request)
        except anthropic.APIError as error:
            raise LLMError(f"Claude API request failed: {error.message}") from error

        text = "".join(block.text for block in response.content if block.type == "text")
        logger.debug("response (stop_reason=%s):\n%s", response.stop_reason, text)
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined to answer this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("the reply was cut off; raise PATCHPILOT_MAX_TOKENS")
        # Validate here, not with messages.parse(), so we keep the response if validation fails.
        try:
            return schema.model_validate_json(text)
        except ValidationError as error:
            raise LLMError(
                f"the reply did not match the expected JSON structure "
                f"({error.error_count()} validation errors)"
            ) from error
