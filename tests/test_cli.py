import json
from pathlib import Path

import pytest
from conftest import COMMENT, DIFF, FakeAPI, reply
from typer.testing import CliRunner

from patchpilot import cli
from patchpilot.config import Settings
from patchpilot.llm import LLMClient

runner = CliRunner()


@pytest.fixture
def fake_cli(api: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    """Make the CLI build clients that talk to the fake API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def build(settings: Settings) -> LLMClient:
        return api.llm(settings)

    monkeypatch.setattr(cli, "LLMClient", build)
    return api


def write_diff(directory: Path, text: str = DIFF) -> str:
    path = directory / "change.diff"
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def test_review_prints_json(fake_cli: FakeAPI, tmp_path: Path) -> None:
    fake_cli.responses = [reply({"comments": [COMMENT]})]

    result = runner.invoke(cli.app, ["review", write_diff(tmp_path)])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"comments": [COMMENT]}
    assert not cli.DEBUG_LOG.exists()


def test_review_accepts_a_byte_order_mark(fake_cli: FakeAPI, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["review", write_diff(tmp_path, "﻿" + DIFF)])

    assert result.exit_code == 0


def test_prompt_version_option(fake_cli: FakeAPI, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["review", write_diff(tmp_path), "--prompt-version", "v1"])

    assert result.exit_code == 0
    assert str(fake_cli.sent_body()["system"]).startswith("You are a code reviewer")


def test_debug_writes_a_log(fake_cli: FakeAPI, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["review", write_diff(tmp_path), "--debug"])

    assert result.exit_code == 0
    log = cli.DEBUG_LOG.read_text(encoding="utf-8")
    assert "sum(items[1:])" in log
    assert "test-key" not in log


@pytest.mark.parametrize(
    ("text", "arguments", "message"),
    [
        ("not a diff\n", [], "expected a 'diff --git' header"),
        (DIFF, ["--prompt-version", "v9"], "prompt_version"),
    ],
)
def test_errors_exit_with_a_message(
    fake_cli: FakeAPI, tmp_path: Path, text: str, arguments: list[str], message: str
) -> None:
    result = runner.invoke(cli.app, ["review", write_diff(tmp_path, text), *arguments])

    assert result.exit_code == 1
    assert message in result.stderr
    assert fake_cli.requests == []


def test_missing_api_key_is_explained(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["review", write_diff(tmp_path)])

    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY is not set" in result.stderr


def test_missing_diff_file() -> None:
    assert runner.invoke(cli.app, ["review", "absent.diff"]).exit_code == 2
