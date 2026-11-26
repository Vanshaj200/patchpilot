"""Tests of the eval harness. Replies are faked, so no number here is a benchmark result."""

import json
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from conftest import FakeAPI, reply

from patchpilot.config import Settings
from patchpilot.diff import ChangeType, new_file_diff, parse_diff
from patchpilot.evaluation import (
    Case,
    CaseScore,
    evaluate_case,
    render_markdown,
    run_evaluation,
    update_readme,
    write_results,
)

CASE = Case(
    name="01_demo",
    filename="calc.py",
    bug_type="wrong operator",
    bug_lines=frozenset({2}),
    good="def add(a, b):\n    return a + b\n",
    buggy="def add(a, b):\n    return a - b\n",
)
TESTS = """\
from calc import add


def test_add():
    assert add(2, 1) == 3


def test_wrong_expectation():
    assert add(2, 1) == 99
"""
SETTINGS = Settings(ANTHROPIC_API_KEY="test-key", response_cache=False)


def comment(line: int) -> dict[str, object]:
    return {"file": "calc.py", "line": line, "severity": "high", "message": "m"}


def test_new_file_diff_parses_back() -> None:
    [changed] = parse_diff(new_file_diff("calc.py", CASE.buggy))

    assert (changed.change_type, changed.path) == (ChangeType.ADDED, "calc.py")
    assert [line.content for line in changed.hunks[0].lines] == [
        "def add(a, b):",
        "    return a - b",
    ]


def test_correct_code_is_never_sent(api: FakeAPI) -> None:
    api.responses = [reply({"comments": []}), reply({"test_code": TESTS})]

    evaluate_case(CASE, "baseline", SETTINGS, True, api.http_client())

    assert "a + b" not in "".join(r.content.decode() for r in api.requests)


def test_scores_review_and_tests(api: FakeAPI) -> None:
    api.responses = [reply({"comments": [comment(2)]}), reply({"test_code": TESTS})]

    score = evaluate_case(CASE, "baseline", SETTINGS, True, api.http_client())

    assert score.caught_by_review
    assert score.caught_by_tests
    assert (score.comments, score.tests, score.bad_tests) == (1, 2, 1)
    assert (score.requests, score.input_tokens, score.output_tokens) == (2, 200, 40)
    assert score.cost_usd == pytest.approx(2 * (100 * 4 + 20 * 20) / 1e6)


def test_the_models_work_is_saved_for_checking(api: FakeAPI) -> None:
    api.responses = [reply({"comments": [comment(1), comment(2)]}), reply({"test_code": TESTS})]

    score = evaluate_case(CASE, "baseline", SETTINGS, True, api.http_client())

    assert [(note["line"], note["on_bug_line"]) for note in score.review_comments] == [
        (1, False),
        (2, True),
    ]
    assert score.review_comments[0]["message"] == "m"
    assert score.test_code == TESTS
    verdicts = {item["test"]: item["verdict"] for item in score.test_verdicts}
    assert verdicts == {"test_add": "possible bug", "test_wrong_expectation": "bad test"}


def test_account_names_are_removed_from_errors(api: FakeAPI) -> None:
    message = "Rate limit reached in organization `org_01abcXYZ` on tokens per minute"
    error = {"type": "error", "error": {"type": "rate_limit_error", "message": message}}
    api.responses = [httpx2.Response(429, json=error, headers={"retry-after-ms": "1"})]

    score = evaluate_case(CASE, "baseline", SETTINGS, False, api.http_client())

    assert "org_***" in score.errors[0]
    assert "org_01abcXYZ" not in score.errors[0]


def test_comment_on_another_line_is_not_a_catch(api: FakeAPI) -> None:
    api.responses = [reply({"comments": [comment(1)]})]

    score = evaluate_case(CASE, "baseline", SETTINGS, False, api.http_client())

    assert not score.caught_by_review
    assert score.caught_by_tests is None
    assert len(api.requests) == 1


def test_failed_requests_are_noted_and_do_not_stop_the_case(api: FakeAPI) -> None:
    api.responses = [reply("broken"), reply("broken")]

    score = evaluate_case(CASE, "baseline", SETTINGS, True, api.http_client())

    assert [error.split(":")[0] for error in score.errors] == ["review", "gentests"]
    assert not score.caught_by_review
    assert score.caught_by_tests is False


def test_both_configurations_run_with_their_own_settings(api: FakeAPI) -> None:
    progress: list[int] = []

    scores = run_evaluation(
        [CASE], SETTINGS, False, lambda s: progress.append(len(s)), api.http_client()
    )

    assert [score.configuration for score in scores] == ["baseline", "optimised"]
    assert progress == [1, 2]
    baseline, optimised = api.sent_body(0), api.sent_body(1)
    assert baseline["model"] == "claude-opus-5-5"
    assert "cache_control" not in api.requests[0].content.decode()
    assert optimised["model"] == "claude-haiku-5-5"
    assert "cache_control" in api.requests[1].content.decode()


SCORE = CaseScore(
    "baseline", "01_demo", "wrong operator", True, False, 1, 2, 0,
    ("m",), 2, 0, 1000, 0, 200, 0.02, 3.0, (),
)  # fmt: skip


def test_table_totals_and_cost_per_bug() -> None:
    scores = [SCORE, replace(SCORE, case="02_x", caught_by_review=False, cost_usd=0.04)]

    table = render_markdown(scores, {"Run": "now"})

    assert "| baseline | 2 | 1 | 0 | 1 | 2000 | 0 | 400 | 0.0600 | 0.0600 | 6.0 | 0 |" in table
    assert "| 01_demo | wrong operator | yes / no |" in table


@pytest.mark.parametrize(
    ("score", "cells"),
    [
        (replace(SCORE, cost_usd=None), "| unknown | n/a |"),
        (replace(SCORE, caught_by_review=False), "| 0.0200 | n/a |"),
        (replace(SCORE, caught_by_tests=None), "| not run |"),
    ],
)
def test_table_never_guesses(score: CaseScore, cells: str) -> None:
    assert cells in render_markdown([score], {})


def test_partial_run_shows_missing_cells() -> None:
    scores = [SCORE, replace(SCORE, case="02_x"), replace(SCORE, configuration="optimised")]

    assert "| 02_x | wrong operator | yes / no | not run |" in render_markdown(scores, {})


def test_results_are_saved(tmp_path: Path) -> None:
    path = write_results([SCORE], {"Run": "now"}, tmp_path / "out", "run1")

    assert path.read_text().startswith("Run: now")
    data = json.loads((tmp_path / "out" / "run1.json").read_text())
    assert data["scores"][0]["case"] == "01_demo"


def test_readme_section_is_replaced(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("top\n<!-- eval:start -->\nold\n<!-- eval:end -->\nbottom\n")

    update_readme(readme, "new\n")

    assert readme.read_text() == ("top\n<!-- eval:start -->\nnew\n<!-- eval:end -->\nbottom\n")


def test_readme_without_markers_is_left_alone(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("no markers\n")

    with pytest.raises(ValueError, match="markers"):
        update_readme(readme, "new\n")

    assert readme.read_text() == "no markers\n"
