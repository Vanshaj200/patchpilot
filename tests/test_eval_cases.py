"""Prove that every seeded bug is real: its oracle test passes on the good code only."""

from pathlib import Path

import pytest

from patchpilot.evaluation import Case, load_cases
from patchpilot.runner import Outcome, run_tests

CASES_DIR = Path(__file__).parent.parent / "eval" / "cases"
CASES = load_cases(CASES_DIR)


def test_there_are_enough_distinct_cases() -> None:
    assert len(CASES) >= 20
    assert len({case.bug_type for case in CASES}) >= 20
    # A repeated file name would not matter now, but would make results harder to read.
    assert len({case.filename for case in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_case_is_well_formed(case: Case) -> None:
    line_count = len(case.buggy.rstrip("\n").split("\n"))

    assert case.good != case.buggy
    assert case.bug_lines
    assert all(1 <= line <= line_count for line in case.bug_lines)
    assert "#!" not in case.buggy


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_oracle_separates_good_from_buggy(case: Case, tmp_path: Path) -> None:
    oracle = (CASES_DIR / case.name / "test_oracle.py").read_text(encoding="utf-8")
    results = {}
    for version, content in (("good", case.good), ("buggy", case.buggy)):
        folder = tmp_path / version
        folder.mkdir()
        (folder / case.filename).write_text(content, encoding="utf-8")
        results[version] = run_tests(oracle, folder, timeout_seconds=60)

    assert [c.outcome for c in results["good"].cases] == [Outcome.PASSED], results["good"].output
    assert [c.outcome for c in results["buggy"].cases] == [Outcome.FAILED], results["buggy"].output
