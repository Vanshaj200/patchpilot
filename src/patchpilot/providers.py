"""The two model APIs. Each backend sends one prompt and returns a Reply in a common form."""

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
import httpx2
import openai
from pydantic import SecretStr

from patchpilot.cache import text_block
from patchpilot.config import Settings
from patchpilot.cost import TokenUsage

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# These Claude models accept server-side refusal fallbacks: if a safety check declines a request
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
_THINKING = re.compile(r"(?s)<think>.*?</think>")


class LLMError(Exception):
    """A request failed or returned something unusable. `status` is stored in the usage log."""

    def __init__(self, message: str, status: str = "error", request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id


@dataclass(frozen=True)
class Prompt:
    model: str
    system: str
    user: str
    schema: dict[str, object]  # JSON schema the reply must match


@dataclass(frozen=True)
class Reply:
    text: str
    status: str  # "ok", "refusal" or "max_tokens"
    request_id: str
    model: str
    # Each model that worked on the request, with its token counts.
    attempts: tuple[tuple[str, TokenUsage], ...]


class Backend(Protocol):
    def wait_for_turn(self) -> None:
        """Pause if the API's rate limit requires it. Not counted as response time."""

    def send(self, prompt: Prompt) -> Reply:
        """Send one request. Raise LLMError if no reply arrives."""


def create_backend(settings: Settings, http_client: httpx2.Client | None) -> Backend:
    if settings.provider == "nvidia":
        return NvidiaBackend(settings, http_client)
    return AnthropicBackend(settings, http_client)


class AnthropicBackend:
    """Claude, with a JSON schema enforced by the API."""

    def __init__(self, settings: Settings, http_client: httpx2.Client | None) -> None:
        self._settings = settings
        # The SDK retries connection errors, timeouts, rate limits and server errors with
        # backoff. A second retry loop here would multiply the number of attempts.
        self._client = anthropic.Anthropic(
            api_key=_require(settings.api_key, "ANTHROPIC_API_KEY"),
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    def wait_for_turn(self) -> None:
        """The SDK's retries handle Claude's rate limits."""

    def send(self, prompt: Prompt) -> Reply:
        cache_system = self._settings.prompt_cache
        cache_user = cache_system and self._settings.cache_file_context
        request: dict[str, Any] = {
            "model": prompt.model,
            "max_tokens": self._settings.max_tokens,
            "system": [text_block(prompt.system, cache_system)],
            "messages": [{"role": "user", "content": [text_block(prompt.user, cache_user)]}],
            "output_config": {
                "effort": self._settings.effort,
                "format": {"type": "json_schema", "schema": prompt.schema},
            },
        }
        if prompt.model in _FALLBACK_MODELS:
            request |= {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
        try:
            response = self._client.beta.messages.create(**request)
        except anthropic.APIError as error:
            raise LLMError(
                f"Claude API request failed: {error.message}",
                status="api_error",
                request_id=getattr(error, "request_id", None),
            ) from error

        # After a refusal fallback, `iterations` has one entry per model that ran, while the
        # top-level usage covers only the last one.
        entries = response.usage.iterations or [response.usage]
        attempts = tuple(
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
        )
        stop = response.stop_reason
        return Reply(
            text="".join(block.text for block in response.content if block.type == "text"),
            status=stop if stop in ("refusal", "max_tokens") else "ok",
            request_id=response._request_id or response.id,
            model=response.model,
            attempts=attempts,
        )


class NvidiaBackend:
    """Models hosted by NVIDIA, through its OpenAI-compatible API.

    The API documentation for these models lists no JSON output option, so the schema is
    described in the prompt. Nothing enforces it until the reply is validated.
    """

    # Shared by all instances: the rate limit applies to the key, not to one object.
    _next_start = 0.0

    def __init__(self, settings: Settings, http_client: httpx2.Client | None) -> None:
        self._max_tokens = settings.max_tokens
        per_minute = settings.nvidia_requests_per_minute
        self._interval = 60 / per_minute if per_minute else 0.0
        self._client = openai.OpenAI(
            api_key=_require(settings.nvidia_api_key, "NVIDIA_API_KEY"),
            base_url=NVIDIA_BASE_URL,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
            http_client=http_client,
        )

    def wait_for_turn(self) -> None:
        if not self._interval:
            return
        delay = NvidiaBackend._next_start - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        NvidiaBackend._next_start = time.monotonic() + self._interval

    def send(self, prompt: Prompt) -> Reply:
        system = (
            f"{prompt.system}\n\nReply with only one JSON object, and no other text, that "
            f"matches this JSON schema:\n{json.dumps(prompt.schema)}"
        )
        try:
            response = self._client.chat.completions.create(
                model=prompt.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt.user},
                ],
                max_tokens=self._max_tokens,
                stream=False,  # NVIDIA streams unless told otherwise
            )
        except openai.APIError as error:
            raise LLMError(
                f"NVIDIA API request failed: {error.message}",
                status="api_error",
                request_id=getattr(error, "request_id", None),
            ) from error
        if not response.choices:
            raise LLMError("NVIDIA API returned no reply", status="api_error")

        choice = response.choices[0]
        statuses = {"length": "max_tokens", "content_filter": "refusal"}
        attempts: tuple[tuple[str, TokenUsage], ...] = ()
        if response.usage is not None:
            details = response.usage.prompt_tokens_details
            cached = (details.cached_tokens or 0) if details else 0
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens - cached,
                output_tokens=response.usage.completion_tokens,
                cache_read_tokens=cached,
            )
            attempts = ((response.model, usage),)
        return Reply(
            text=_json_object(choice.message.content or ""),
            status=statuses.get(choice.finish_reason, "ok"),
            request_id=response._request_id or response.id,
            model=response.model,
            attempts=attempts,
        )


def _json_object(text: str) -> str:
    """Cut the JSON object out of a reply that may add reasoning, code fences or remarks."""
    text = _THINKING.sub("", text)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def _require(key: SecretStr | None, name: str) -> str:
    if key is None or not key.get_secret_value():
        raise LLMError(f"{name} is not set")
    return key.get_secret_value()
