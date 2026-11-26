import json
from pathlib import Path

import pytest
from conftest import COMMENT, DIFF, FakeAPI, reply
from typer.testing import CliRunner

from patchpilot import cli
from patchpilot.config import Settings
from patchpilot.llm import LLMClient, Recorder

runner = CliRunner()


@pytest.fixture
def fake_cli(api: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    """Make the CLI build clients that talk to the fake API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def build(settings: Settings, on_request: Recorder) -> LLMClient:
        return LLMClient(settings, on_request, http_client=api.http_client())

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


def test_report_totals_logged_requests(fake_cli: FakeAPI, tmp_path: Path) -> None:
    diff = write_diff(tmp_path)
    runner.invoke(cli.app, ["review", diff])
    runner.invoke(cli.app, ["review", diff])

    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 0
    total = next(line for line in result.stdout.splitlines() if line.startswith("Total"))
    # Two requests of 100 input and 20 output tokens at $4 and $20 per million.
    assert total.split() == ["Total", "2", "200", "40", "0", "0", "0.001600"]
    assert "no known cost" not in result.stdout


def test_report_mentions_unknown_costs(fake_cli: FakeAPI, tmp_path: Path) -> None:
    fake_cli.responses = [reply(model="claude-unreleased")]
    runner.invoke(cli.app, ["review", write_diff(tmp_path)])

    result = runner.invoke(cli.app, ["report"])

    assert "1 requests have no known cost" in result.stdout


def test_report_without_data() -> None:
    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 0
    assert "No requests logged" in result.stdout


def test_report_with_a_corrupt_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "bad.db").write_text("this is not a database, " * 20)
    monkeypatch.setenv("PATCHPILOT_DB_PATH", "bad.db")

    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_gentests_writes_the_file_without_running_it(fake_cli: FakeAPI, tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def total(items):\n    return sum(items[1:])\n")
    code = "def test_total():\n    raise SystemExit('must not run')\n"
    fake_cli.responses = [reply({"test_code": code})]

    result = runner.invoke(cli.app, ["gentests", write_diff(tmp_path), "--output", "out/t.py"])

    assert result.exit_code == 0
    assert (tmp_path / "out" / "t.py").read_text() == code
    assert "patchpilot run" in result.stdout


def test_gentests_without_changed_functions(fake_cli: FakeAPI, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["gentests", write_diff(tmp_path)])

    assert result.exit_code == 0
    assert "nothing was written" in result.stdout
    assert not (tmp_path / "patchpilot_tests.py").exists()


def make_project(directory: Path, body: str) -> str:
    directory.mkdir()
    (directory / "calc.py").write_text(f"def total(items):\n    return {body}\n")
    return str(directory)


CALC_TESTS = """\
from calc import total


def test_sum():
    assert total([1, 2]) == 3


def test_wrong_expectation():
    assert total([]) == 1
"""


def test_run_separates_bugs_from_bad_tests(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text(CALC_TESTS)
    buggy = make_project(tmp_path / "buggy", "sum(items[1:])")
    good = make_project(tmp_path / "good", "sum(items)")

    result = runner.invoke(cli.app, ["run", "t.py", "--repo", buggy, "--good-repo", good])

    assert result.exit_code == 1
    assert "possible bug  test_sum" in result.stdout
    assert "bad test      test_wrong_expectation" in result.stdout


def test_run_succeeds_when_only_bad_tests_fail(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text(CALC_TESTS)
    good = make_project(tmp_path / "good", "sum(items)")

    result = runner.invoke(cli.app, ["run", "t.py", "--repo", good, "--good-repo", good])

    assert result.exit_code == 0
    assert "1 passed, 1 bad test" in result.stdout


def test_run_reports_a_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATCHPILOT_TEST_TIMEOUT_SECONDS", "2")
    (tmp_path / "t.py").write_text("import time\n\ndef test_slow():\n    time.sleep(60)\n")
    (tmp_path / "code").mkdir()

    result = runner.invoke(cli.app, ["run", "t.py", "--repo", "code"])

    assert result.exit_code == 1
    assert "timed out after 2 s" in result.stderr


def test_missing_diff_file() -> None:
    assert runner.invoke(cli.app, ["review", "absent.diff"]).exit_code == 2
