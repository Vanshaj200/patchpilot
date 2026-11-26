"""Settings read from environment variables and an optional .env file."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Each field is set by PATCHPILOT_<NAME>, except the API key. Real variables beat .env."""

    model_config = SettingsConfigDict(env_prefix="PATCHPILOT_", env_file=".env", extra="ignore")

    # SecretStr prints as "**********", so the key cannot leak through logs or error messages.
    api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Upper limit on the reply, including the model's thinking.
    max_tokens: int = Field(default=16_000, gt=0)
    timeout_seconds: float = Field(default=180.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=2)
    prompt_version: Literal["v1", "v2"] = "v2"
    # A cost guard: refuse to send an unexpectedly large prompt.
    max_prompt_chars: int = Field(default=200_000, gt=0)
