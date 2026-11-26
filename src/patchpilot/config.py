"""Settings read from environment variables and an optional .env file, and the price table."""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPrice(BaseModel):
    """US dollars per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write: float = Field(ge=0)  # 5-minute cache lifetime
    cache_read: float = Field(ge=0)
    # Some models charge more for the whole request when the prompt is longer than this.
    long_prompt_over: int | None = None
    long_prompt_multiplier: float = 1.0


# Checked against https://platform.claude.com/docs/en/about-claude/pricing on 2026-11-26.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(input=4, output=20, cache_write=5, cache_read=0.20),
    "claude-opus-5": ModelPrice(input=5, output=25, cache_write=6.25, cache_read=0.50),
    "claude-opus-4-8": ModelPrice(input=5, output=25, cache_write=6.25, cache_read=0.50),
    "claude-sonnet-5-5": ModelPrice(input=2, output=10, cache_write=2.50, cache_read=0.10),
    "claude-sonnet-5": ModelPrice(input=2, output=10, cache_write=2.50, cache_read=0.20),
    "claude-haiku-5-5": ModelPrice(
        input=0.10,
        output=0.50,
        cache_write=0.125,
        cache_read=0.01,
        long_prompt_over=100_000,
        long_prompt_multiplier=5,
    ),
    "claude-haiku-4-5": ModelPrice(input=1, output=5, cache_write=1.25, cache_read=0.10),
}


class Settings(BaseSettings):
    """Each field is set by PATCHPILOT_<NAME>, except the API key. Real variables beat .env."""

    model_config = SettingsConfigDict(env_prefix="PATCHPILOT_", env_file=".env", extra="ignore")

    # SecretStr prints as "**********", so the key cannot leak through logs or error messages.
    api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-opus-5-5"  # the large model
    small_model: str = "claude-haiku-5-5"
    # Routing sends a change to the small model only if it passes all three rules.
    routing: bool = True
    route_max_files: int = Field(default=1, ge=0)
    route_max_changed_lines: int = Field(default=20, ge=0)
    route_risky_pattern: re.Pattern[str] = re.compile(
        r"(?i)password|secret|token|\bauth|crypt|sql|execute|subprocess|eval\(|exec\(|pickle"
        r"|thread|async"
    )
    # Prompt caching marks the system prompt. Marking the diff too costs 25% more on those
    # tokens and only pays off if the identical diff is sent again within five minutes.
    prompt_cache: bool = True
    cache_file_context: bool = False
    response_cache: bool = True
    cache_dir: Path = Path(".patchpilot/cache")
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Upper limit on the reply, including the model's thinking.
    max_tokens: int = Field(default=16_000, gt=0)
    timeout_seconds: float = Field(default=180.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=2)
    prompt_version: Literal["v1", "v2"] = "v2"
    # A cost guard: refuse to send an unexpectedly large prompt.
    max_prompt_chars: int = Field(default=200_000, gt=0)
    # Time limit for one run of the generated tests.
    test_timeout_seconds: float = Field(default=60.0, gt=0)
    db_path: Path = Path(".patchpilot/usage.db")
    # Override the whole table with JSON in PATCHPILOT_PRICES.
    prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(PRICES))
