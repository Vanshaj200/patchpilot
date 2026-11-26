from pathlib import Path

import pytest

from patchpilot.runner import CaseResult, Outcome, RunResult, Verdict, judge, run_tests

TESTS = """\
from calc import double


def test_double():
    assert double(2) == 4


def test_zero():
    assert double(0) == 0
"""


def project(tmp_path: Path, name: str, body: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "calc.py").write_text(f"def double(x):\n    return {body}\n")
    return directory


def outcomes(result: RunResult) -> dict[str, Outcome]:
    return {case.name: case.outcome for case in result.cases}


def test_reports_each_test(tmp_path: Path) -> None:
    result = run_tests(TESTS, project(tmp_path, "buggy", "x + 2"), timeout_seconds=60)

    assert outcomes(result) == {"test_double": Outcome.PASSED, "test_zero": Outcome.FAILED}
    assert "assert" in next(c.message for c in result.cases if c.name == "test_zero")
    assert not result.timed_out


def test_code_in_a_src_folder_can_be_imported(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def double(x):\n    return x * 2\n")

    result = run_tests(TESTS, tmp_path, timeout_seconds=60)

    assert set(outcomes(result).values()) == {Outcome.PASSED}


def test_import_error_is_an_error(tmp_path: Path) -> None:
    result = run_tests("import missing_module\n\ndef test_x():\n    pass\n", tmp_path, 60)

    assert [case.outcome for case in result.cases] == [Outcome.ERROR]


def test_slow_tests_are_stopped(tmp_path: Path) -> None:
    code = "def test_loop():\n    while True:\n        pass\n"

    result = run_tests(code, tmp_path, timeout_seconds=3)

    assert result.timed_out
    assert result.cases == ()


def test_secrets_are_not_visible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=secret\n")
    code = """\
import os
from pathlib import Path


def test_no_secrets():
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert not Path(".env").exists()
"""

    result = run_tests(code, tmp_path, timeout_seconds=60)

    assert outcomes(result) == {"test_no_secrets": Outcome.PASSED}


def test_the_original_folder_is_not_changed(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("original")
    code = """\
from pathlib import Path


def test_overwrite():
    Path("data.txt").write_text("changed")
"""

    run_tests(code, tmp_path, timeout_seconds=60)

    assert (tmp_path / "data.txt").read_text() == "original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data.txt"]


def test_project_conftest_and_config_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text("raise RuntimeError('should not load')\n")
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "--bad"\n')

    result = run_tests("def test_ok():\n    pass\n", tmp_path, timeout_seconds=60)

    assert outcomes(result) == {"test_ok": Outcome.PASSED}


def test_class_based_tests_keep_distinct_names(tmp_path: Path) -> None:
    code = "class TestA:\n    def test_x(self):\n        pass\n\ndef test_x():\n    pass\n"

    result = run_tests(code, tmp_path, timeout_seconds=60)

    assert len(result.cases) == 2


def result_of(**cases: Outcome) -> RunResult:
    return RunResult(
        tuple(CaseResult(name, outcome, "") for name, outcome in cases.items()), False, ""
    )


def test_judge_without_known_good_code() -> None:
    target = result_of(a=Outcome.PASSED, b=Outcome.FAILED, c=Outcome.ERROR, d=Outcome.SKIPPED)

    assert [verdict for _, verdict in judge(target, None)] == [
        Verdict.PASSED,
        Verdict.FAILED,
        Verdict.FAILED,
        Verdict.SKIPPED,
    ]


@pytest.mark.parametrize(
    ("on_target", "on_good", "verdict"),
    [
        (Outcome.PASSED, Outcome.PASSED, Verdict.PASSED),
        (Outcome.FAILED, Outcome.PASSED, Verdict.POSSIBLE_BUG),
        (Outcome.FAILED, Outcome.FAILED, Verdict.BAD_TEST),
        (Outcome.PASSED, Outcome.ERROR, Verdict.BAD_TEST),
    ],
)
def test_judge_against_known_good_code(
    on_target: Outcome, on_good: Outcome, verdict: Verdict
) -> None:
    judged = judge(result_of(a=on_target), result_of(a=on_good))

    assert [found for _, found in judged] == [verdict]


def test_judge_when_the_known_good_run_has_no_result() -> None:
    judged = judge(result_of(a=Outcome.FAILED), RunResult((), True, ""))

    assert [verdict for _, verdict in judged] == [Verdict.UNVERIFIED]
