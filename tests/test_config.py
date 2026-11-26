from pathlib import Path

import pytest
from pydantic import ValidationError

from patchpilot.config import Settings


def test_defaults() -> None:
    settings = Settings()

    assert settings.api_key is None
    assert settings.model == "claude-opus-5-5"
    assert settings.max_retries == 2


def test_reads_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value")
    monkeypatch.setenv("PATCHPILOT_MODEL", "claude-haiku-5-5")

    settings = Settings()

    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "secret-value"
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
