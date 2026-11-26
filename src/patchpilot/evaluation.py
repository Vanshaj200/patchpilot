"""Measure how many seeded bugs PatchPilot catches, and what that costs.

Each case is shown to Claude as a newly added file holding the buggy code, so the correct
version is never revealed. A bug counts as caught by review if a comment points at one of the
case's bug lines, and caught by tests if a generated test passes on the correct code and fails
on the buggy code.
"""

import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx2

from patchpilot.config import Settings
from patchpilot.diff import new_file_diff, parse_diff
from patchpilot.gentests import generate_tests
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import review_diff
from patchpilot.runner import Verdict, judge, run_tests
from patchpilot.store import RequestRecord

# The settings that differ between the two runs. Everything else is shared.
CONFIGURATIONS: dict[str, dict[str, bool]] = {
    "baseline": {"routing": False, "prompt_cache": False, "response_cache": False},
    "optimised": {"routing": True, "prompt_cache": True, "response_cache": True},
}
README_START = "<!-- eval:start -->"
README_END = "<!-- eval:end -->"


@dataclass(frozen=True)
class Case:
    name: str
    filename: str
    bug_type: str
    bug_lines: frozenset[int]
    good: str
    buggy: str


@dataclass(frozen=True)
class CaseScore:
    """What happened for one case under one configuration."""

    configuration: str
    case: str
    bug_type: str
    caught_by_review: bool
    caught_by_tests: bool | None  # None when test generation was switched off
    comments: int
    tests: int
    bad_tests: int
    models: tuple[str, ...]
    requests: int
    cache_hits: int
    input_tokens: int  # all input, including cached
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_seconds: float
    errors: tuple[str, ...]


def load_cases(directory: Path) -> list[Case]:
    cases: list[Case] = []
    for info_file in sorted(directory.glob("*/case.json")):
        folder = info_file.parent
        info = json.loads(info_file.read_text(encoding="utf-8"))
        cases.append(
            Case(
                name=folder.name,
                filename=info["filename"],
                bug_type=info["bug_type"],
                bug_lines=frozenset(info["bug_lines"]),
                good=(folder / "good.py").read_text(encoding="utf-8"),
                buggy=(folder / "buggy.py").read_text(encoding="utf-8"),
            )
        )
    return cases


def evaluate_case(
    case: Case,
    configuration: str,
    settings: Settings,
    with_tests: bool,
    http_client: httpx2.Client | None = None,
) -> CaseScore:
    records: list[RequestRecord] = []
    errors: list[str] = []
    llm = LLMClient(settings, on_request=records.append, http_client=http_client)
    files = parse_diff(new_file_diff(case.filename, case.buggy))
    caught_by_review, comments = False, 0
    caught_by_tests = False if with_tests else None
    tests = bad_tests = 0

    with tempfile.TemporaryDirectory(prefix="patchpilot-eval-") as temp:
        buggy_dir, good_dir = Path(temp) / "buggy", Path(temp) / "good"
        for folder, content in ((buggy_dir, case.buggy), (good_dir, case.good)):
            folder.mkdir()
            (folder / case.filename).write_text(content, encoding="utf-8")

        # A failed request is recorded and the run continues: earlier results were paid for.
        try:
            review = review_diff(files, llm, settings, buggy_dir)
            comments = len(review.comments)
            caught_by_review = any(c.line in case.bug_lines for c in review.comments)
        except LLMError as error:
            errors.append(f"review: {error}")

        if with_tests:
            try:
                code = generate_tests(files, buggy_dir, llm, settings)
                if code is not None:
                    timeout = settings.test_timeout_seconds
                    judged = judge(
                        run_tests(code, buggy_dir, timeout), run_tests(code, good_dir, timeout)
                    )
                    tests = len(judged)
                    bad_tests = sum(verdict == Verdict.BAD_TEST for _, verdict in judged)
                    caught_by_tests = any(verdict == Verdict.POSSIBLE_BUG for _, verdict in judged)
            except LLMError as error:
                errors.append(f"gentests: {error}")

    costs = [record.cost_usd for record in records]
    known = [cost for cost in costs if cost is not None]
    return CaseScore(
        configuration=configuration,
        case=case.name,
        bug_type=case.bug_type,
        caught_by_review=caught_by_review,
        caught_by_tests=caught_by_tests,
        comments=comments,
        tests=tests,
        bad_tests=bad_tests,
        models=tuple(sorted({record.model for record in records})),
        requests=len(records),
        cache_hits=sum(record.status == "cache_hit" for record in records),
        input_tokens=sum(
            r.input_tokens + r.cache_write_tokens + r.cache_read_tokens for r in records
        ),
        cache_read_tokens=sum(record.cache_read_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        cost_usd=sum(known) if len(known) == len(costs) else None,
        latency_seconds=sum(record.latency_seconds for record in records),
        errors=tuple(errors),
    )


def run_evaluation(
    cases: Sequence[Case],
    settings: Settings,
    with_tests: bool,
    on_score: Callable[[list[CaseScore]], None],
    http_client: httpx2.Client | None = None,
) -> list[CaseScore]:
    """Run every case under every configuration, reporting all scores after each one."""
    scores: list[CaseScore] = []
    for configuration, overrides in CONFIGURATIONS.items():
        # An empty response cache each time, so no reply is carried over from an earlier run.
        with tempfile.TemporaryDirectory(prefix="patchpilot-cache-") as cache_dir:
            configured = settings.model_copy(update=overrides | {"cache_dir": Path(cache_dir)})
            for case in cases:
                scores.append(
                    evaluate_case(case, configuration, configured, with_tests, http_client)
                )
                on_score(scores)
    return scores


def render_markdown(scores: Sequence[CaseScore], details: Mapping[str, str]) -> str:
    """Build the summary table and the per-case table."""
    lines = [", ".join(f"{name}: {value}" for name, value in details.items()), ""]
    lines += [
        "| Configuration | Cases | Caught by review | Caught by tests | Caught (either) "
        "| Input tokens | Cached input | Output tokens | Total cost (USD) "
        "| Cost per caught bug (USD) | API time (s) | Errors |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    configurations = list(dict.fromkeys(score.configuration for score in scores))
    for configuration in configurations:
        rows = [score for score in scores if score.configuration == configuration]
        caught = sum(_caught(row) for row in rows)
        costs = [row.cost_usd for row in rows if row.cost_usd is not None]
        complete = len(costs) == len(rows)
        total = f"{sum(costs):.4f}" if complete else "unknown"
        per_bug = f"{sum(costs) / caught:.4f}" if complete and caught else "n/a"
        if any(row.caught_by_tests is None for row in rows):
            by_tests = "not run"
        else:
            by_tests = str(sum(bool(row.caught_by_tests) for row in rows))
        cells = [
            configuration,
            len(rows),
            sum(row.caught_by_review for row in rows),
            by_tests,
            caught,
            sum(row.input_tokens for row in rows),
            sum(row.cache_read_tokens for row in rows),
            sum(row.output_tokens for row in rows),
            total,
            per_bug,
            f"{sum(row.latency_seconds for row in rows):.1f}",
            sum(len(row.errors) for row in rows),
        ]
        lines.append("| " + " | ".join(map(str, cells)) + " |")

    lines += ["", "Each cell below shows review / tests.", ""]
    lines.append("| Case | Bug type | " + " | ".join(configurations) + " |")
    lines.append("|---|---|" + "---|" * len(configurations))
    by_key = {(score.configuration, score.case): score for score in scores}
    for case, bug_type in dict.fromkeys((s.case, s.bug_type) for s in scores):
        cells = []
        for configuration in configurations:
            score = by_key.get((configuration, case))
            if score is None:
                cells.append("not run")
            else:
                tests = score.caught_by_tests
                cells.append(f"{_yes_no(score.caught_by_review)} / {_yes_no(tests)}")
        lines.append(f"| {case} | {bug_type} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_results(
    scores: Sequence[CaseScore], details: Mapping[str, str], directory: Path, stem: str
) -> Path:
    """Save the raw scores as JSON and the tables as Markdown. Return the Markdown path."""
    directory.mkdir(parents=True, exist_ok=True)
    data = {"details": dict(details), "scores": [asdict(score) for score in scores]}
    (directory / f"{stem}.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    markdown = directory / f"{stem}.md"
    markdown.write_text(render_markdown(scores, details), encoding="utf-8")
    return markdown


def update_readme(readme: Path, table: str) -> None:
    """Replace the text between the eval markers in the README."""
    text = readme.read_text(encoding="utf-8")
    before, start, rest = text.partition(README_START)
    _, end, after = rest.partition(README_END)
    if not start or not end:
        raise ValueError(f"{readme} needs the markers {README_START} and {README_END}")
    readme.write_text(f"{before}{start}\n{table}{end}{after}", encoding="utf-8")


def _caught(score: CaseScore) -> bool:
    return score.caught_by_review or bool(score.caught_by_tests)


def _yes_no(value: bool | None) -> str:
    return "-" if value is None else "yes" if value else "no"
