"""The browser's test runner is plain Python, so it can be checked outside the browser."""

import json
import subprocess
import sys
from pathlib import Path

import patchpilot

HARNESS = Path(patchpilot.__file__).parent / "static" / "harness.py"
CODE = "def double(x):\n    return x * 2\n"


def run_harness(tmp_path: Path, tests: str, code: str = CODE) -> dict[str, object]:
    # A separate process: the harness changes the working folder and imports the code.
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); import harness; "
        "print(harness.run(sys.argv[2], sys.argv[3], sys.argv[4]))"
    )
    folder = str(tmp_path / "work")
    command = [sys.executable, "-c", script, str(HARNESS.parent), code, tests, folder]
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def outcomes(report: dict[str, object]) -> dict[str, str]:
    return {item["name"]: item["outcome"] for item in report["results"]}


def test_reports_every_outcome(tmp_path: Path) -> None:
    tests = """\
import pytest
from snippet import double


def test_pass():
    assert double(2) == 4


def test_fail():
    assert double(2) == 5


@pytest.mark.skip
def test_skip():
    pass


@pytest.fixture
def broken():
    raise RuntimeError("no setup")


def test_error(broken):
    pass


class TestGroup:
    def test_inside(self):
        pass
"""

    report = run_harness(tmp_path, tests)

    assert outcomes(report) == {
        "test_pass": "passed",
        "test_fail": "failed",
        "test_skip": "skipped",
        "test_error": "error",
        "TestGroup::test_inside": "passed",
    }
    messages = {item["name"]: item["message"] for item in report["results"]}
    assert "assert 4 == 5" in messages["test_fail"]
    assert "no setup" in messages["test_error"]
    assert "1 failed" in report["output"]


def test_failing_cleanup_is_not_a_pass(tmp_path: Path) -> None:
    tests = """\
import pytest


@pytest.fixture
def dirty():
    yield
    raise RuntimeError("cleanup failed")


def test_x(dirty):
    pass
"""

    assert outcomes(run_harness(tmp_path, tests)) == {"test_x": "error"}


def test_code_that_cannot_be_imported_is_an_error(tmp_path: Path) -> None:
    report = run_harness(tmp_path, "from snippet import double\n\ndef test_x():\n    pass\n", "(")

    [result] = report["results"]
    assert result["outcome"] == "error"
    assert "SyntaxError" in result["message"]


def test_no_tests(tmp_path: Path) -> None:
    assert run_harness(tmp_path, "x = 1\n")["results"] == []
