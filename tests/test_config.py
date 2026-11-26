from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from patchpilot.config import PROVIDERS, Settings


def test_defaults() -> None:
    settings = Settings()

    assert settings.anthropic_api_key is None
    assert settings.model == "claude-opus-5-5"
    assert settings.max_retries == 2


def test_reads_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    monkeypatch.setenv("PATCHPILOT_MODEL", "claude-haiku-5-5")

    settings = Settings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "secret-value"
    assert settings.model == "claude-haiku-5-5"


def test_environment_beats_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PATCHPILOT_MODEL=from-file\nPATCHPILOT_MAX_TOKENS=50\n")
    monkeypatch.setenv("PATCHPILOT_MODEL", "from-environment")

    settings = Settings()

    assert (settings.model, settings.max_tokens) == ("from-environment", 50)


def test_api_key_is_hidden_when_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")

    assert "secret-value" not in repr(Settings())


@pytest.mark.parametrize("retries", ["3", "-1"])
def test_retries_are_limited_to_two(monkeypatch: pytest.MonkeyPatch, retries: str) -> None:
    monkeypatch.setenv("PATCHPILOT_MAX_RETRIES", retries)

    with pytest.raises(ValidationError):
        Settings()


def test_every_provider_can_be_selected() -> None:
    allowed = get_args(Settings.model_fields["provider"].annotation)

    assert set(allowed) == set(PROVIDERS)


def test_example_env_file_holds_no_keys() -> None:
    """The example file is committed to git, so a real key must never be saved in it."""
    example = Path(__file__).parent.parent / ".env.example"
    key_lines = [line for line in example.read_text().splitlines() if "_API_KEY=" in line]

    assert key_lines
    assert all(line.strip().endswith("_API_KEY=") for line in key_lines)
