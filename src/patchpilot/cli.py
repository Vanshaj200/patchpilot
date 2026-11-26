"""Command-line interface."""

import io
import json
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from patchpilot import __version__
from patchpilot.config import PROVIDERS, Settings
from patchpilot.diff import ChangedFile, DiffParseError, parse_diff
from patchpilot.evaluation import (
    CONFIGURATIONS,
    CaseScore,
    load_cases,
    render_markdown,
    run_evaluation,
    update_readme,
    write_results,
)
from patchpilot.gentests import generate_tests
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import github_annotations, review_diff
from patchpilot.runner import Verdict, judge, run_tests
from patchpilot.store import SummaryRow, UsageStore

DEBUG_LOG = Path("patchpilot-debug.log")

# Local variables stay out of tracebacks because they can hold source code or the API key.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)

DiffFile = Annotated[
    Path,
    typer.Argument(exists=True, dir_okay=False, help="A file holding the output of `git diff`."),
]
RepoOption = Annotated[
    Path,
    typer.Option(exists=True, file_okay=False, help="Checkout that already contains the change."),
]
PromptOption = Annotated[str | None, typer.Option(help="Prompt version: v1 or v2.")]
ProviderOption = Annotated[str | None, typer.Option(help=f"Model API: {', '.join(PROVIDERS)}.")]
# None means "not given", so the environment setting applies.
RoutingOption = Annotated[
    bool | None,
    typer.Option("--routing/--no-routing", help="Send trivial diffs to the small model."),
]
PromptCacheOption = Annotated[
    bool | None,
    typer.Option("--prompt-cache/--no-prompt-cache", help="Use Anthropic prompt caching."),
]
ResponseCacheOption = Annotated[
    bool | None,
    typer.Option(
        "--response-cache/--no-response-cache", help="Reuse local replies to identical requests."
    ),
]
DebugOption = Annotated[
    bool,
    typer.Option("--debug", help=f"Write prompts and replies, including code, to {DEBUG_LOG}."),
]


class OutputFormat(StrEnum):
    JSON = "json"
    GITHUB = "github"


@app.callback()
def main() -> None:
    """Review a git diff with a language model and test the changed code."""
    # Test messages and replies can hold characters that a piped Windows console cannot
    # encode. Show them escaped instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


@app.command()
def review(
    diff_file: DiffFile,
    repo: RepoOption = Path("."),
    output_format: Annotated[
        OutputFormat, typer.Option("--format", help="json, or github for pull request notes.")
    ] = OutputFormat.JSON,
    provider: ProviderOption = None,
    prompt_version: PromptOption = None,
    routing: RoutingOption = None,
    prompt_cache: PromptCacheOption = None,
    response_cache: ResponseCacheOption = None,
    debug: DebugOption = False,
) -> None:
    """Print review comments for a diff."""
    if debug:
        _enable_debug_log()
    with _report_errors():
        settings = _load_settings(
            provider=provider,
            prompt_version=prompt_version,
            routing=routing,
            prompt_cache=prompt_cache,
            response_cache=response_cache,
        )
        files = _read_diff(diff_file)
        result = review_diff(files, _client(settings), settings, repo)
    if output_format == OutputFormat.GITHUB:
        typer.echo(github_annotations(result))
    else:
        # json.dumps escapes non-ASCII text, so output is safe on any console encoding.
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@app.command()
def gentests(
    diff_file: DiffFile,
    repo: RepoOption = Path("."),
    output: Annotated[Path, typer.Option(help="Where to write the tests.")] = Path(
        "patchpilot_tests.py"
    ),
    provider: ProviderOption = None,
    prompt_version: PromptOption = None,
    routing: RoutingOption = None,
    prompt_cache: PromptCacheOption = None,
    response_cache: ResponseCacheOption = None,
    debug: DebugOption = False,
) -> None:
    """Write pytest tests for the changed Python functions. They are not run."""
    if debug:
        _enable_debug_log()
    with _report_errors():
        settings = _load_settings(
            provider=provider,
            prompt_version=prompt_version,
            routing=routing,
            prompt_cache=prompt_cache,
            response_cache=response_cache,
        )
        files = _read_diff(diff_file)
        code = generate_tests(files, repo, _client(settings), settings)
        if code is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(code, encoding="utf-8")
    if code is None:
        typer.echo("No changed Python functions found; nothing was written.")
    else:
        typer.echo(f"Wrote {output}. Read it first, then run: patchpilot run {output}")


@app.command()
def run(
    test_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    repo: RepoOption = Path("."),
    good_repo: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=False,
            help="Checkout of code known to be correct. Tests failing there are bad tests.",
        ),
    ] = None,
) -> None:
    """Run generated tests on a temporary copy of the code. Exits with 1 if any fail."""
    with _report_errors():
        timeout = _load_settings().test_timeout_seconds
        code = test_file.read_text(encoding="utf-8")
        target = run_tests(code, repo, timeout)
        known_good = None if good_repo is None else run_tests(code, good_repo, timeout)

    if target.timed_out or not target.cases:
        reason = f"timed out after {timeout:g} s" if target.timed_out else "ran no tests"
        typer.echo(f"The test run {reason}.\n{target.output}", err=True)
        raise typer.Exit(code=1)
    if known_good is not None and (known_good.timed_out or not known_good.cases):
        typer.echo("Warning: the known-good run produced no results.", err=True)

    judged = judge(target, known_good)
    for case, verdict in judged:
        detail = case.message.splitlines()[0][:100] if case.message else ""
        typer.echo(f"{verdict.value:<13} {case.name}  {detail}".rstrip())
    counts = Counter(verdict for _, verdict in judged)
    typer.echo(", ".join(f"{number} {verdict.value}" for verdict, number in counts.items()))
    if counts[Verdict.FAILED] or counts[Verdict.POSSIBLE_BUG]:
        raise typer.Exit(code=1)


@app.command()
def report() -> None:
    """Print total tokens and cost by feature and model."""
    with _report_errors():
        settings = _load_settings()
        rows = UsageStore(settings.db_path).summary()
    typer.echo(_format_report(rows) if rows else f"No requests logged in {settings.db_path}.")


@app.command("eval")
def evaluate(
    cases: Annotated[
        Path, typer.Option(exists=True, file_okay=False, help="Folder of seeded-bug cases.")
    ] = Path("eval/cases"),
    results: Annotated[Path, typer.Option(help="Folder for the result files.")] = Path(
        "eval/results"
    ),
    limit: Annotated[int | None, typer.Option(min=1, help="Run only the first N cases.")] = None,
    tests: Annotated[
        bool, typer.Option("--tests/--no-tests", help="Also generate and run tests.")
    ] = True,
    readme: Annotated[
        Path | None, typer.Option(help="README whose eval section should be replaced.")
    ] = None,
    provider: ProviderOption = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before sending requests.")] = False,
) -> None:
    """Compare baseline and optimised runs on the seeded bugs. API requests may be billed."""
    with _report_errors():
        settings = _load_settings(provider=provider)
        selected = load_cases(cases)[:limit]
        if not selected:
            raise OSError(f"no cases found in {cases}")
        # Fail before the confirmation if no key is configured.
        LLMClient(settings, on_request=lambda record: None)
        requests = len(selected) * len(CONFIGURATIONS) * (2 if tests else 1)
        if not yes:
            question = f"This sends up to {requests} requests to the {settings.provider} API."
            typer.confirm(f"{question} Continue?", abort=True)

        started = datetime.now(UTC)
        details = {
            "Run (UTC)": started.strftime("%Y-%m-%d %H:%M"),
            "PatchPilot": __version__,
            "provider": settings.provider,
            "large model": settings.model,
            "small model": settings.small_model,
            "prompt": settings.prompt_version,
            "effort": settings.effort,
        }
        stem = started.strftime("%Y%m%d-%H%M%S")

        def save(scores: list[CaseScore]) -> None:
            # Saved after every case, so an interrupted run keeps what it paid for.
            write_results(scores, details, results, stem)
            latest = scores[-1]
            caught = latest.caught_by_review or bool(latest.caught_by_tests)
            typer.echo(
                f"{latest.configuration} {latest.case}: {'caught' if caught else 'missed'}"
                + "".join(f" [{error}]" for error in latest.errors),
                err=True,
            )

        scores = run_evaluation(selected, settings, tests, on_score=save)
        table = render_markdown(scores, details)
        if readme is not None:
            update_readme(readme, table)
    typer.echo(table)
    typer.echo(f"Saved {results / stem}.json and .md", err=True)


@app.command()
def web(
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port to listen on.")] = 8000,
) -> None:
    """Open a local web page for reviews with a model and API key of your choice."""
    try:
        # Imported here so the other commands work without the optional web libraries.
        import uvicorn

        from patchpilot.web import create_app
    except ImportError as error:
        typer.echo('Install the web libraries first: pip install -e ".[web]"', err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Open http://127.0.0.1:{port} in your browser. Press Ctrl+C to stop.")
    # No option to listen elsewhere: the server can use API keys saved on this computer.
    uvicorn.run(create_app(), host="127.0.0.1", port=port)


@contextmanager
def _report_errors() -> Iterator[None]:
    """Turn expected failures into a short message and exit code 1."""
    try:
        yield
    except (DiffParseError, LLMError, ValueError, sqlite3.Error, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error


def _client(settings: Settings) -> LLMClient:
    return LLMClient(settings, on_request=UsageStore(settings.db_path).add)


def _format_report(rows: Sequence[SummaryRow]) -> str:
    header = ["Feature", "Model", "Requests", "Cache hits", "Input", "Output"]
    header += ["Cache write", "Cache read"]
    table = [[*header, "Cost (USD)"]]
    for row in rows:
        counts = [row.requests, row.cache_hits, row.input_tokens, row.output_tokens]
        counts += [row.cache_write_tokens, row.cache_read_tokens]
        cost = _format_cost(row.cost_usd, row.unknown_cost_requests)
        table.append([row.feature, row.model, *map(str, counts), cost])
    totals = [sum(int(line[column]) for line in table[1:]) for column in range(2, len(header))]
    unknown = sum(row.unknown_cost_requests for row in rows)
    total_cost = _format_cost(sum(row.cost_usd for row in rows), unknown)
    table.append(["Total", "", *map(str, totals), total_cost])

    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]))]
    lines = [
        "  ".join(
            cell.ljust(width) if column < 2 else cell.rjust(width)
            for column, (cell, width) in enumerate(zip(line, widths, strict=True))
        )
        for line in table
    ]
    saved = sum(row.tokens_saved for row in rows)
    lines.append(f"\nEstimated input tokens saved by not sending whole files: {saved}")
    if unknown:
        lines.append(f"{unknown} requests have no known cost and are not in the cost figures.")
    return "\n".join(lines)


def _format_cost(known_cost: float, unknown_requests: int) -> str:
    """Never show a sum that leaves requests out as if it were complete."""
    if not unknown_requests:
        return f"{known_cost:.6f}"
    return f">={known_cost:.6f}" if known_cost else "unknown"


def _load_settings(**overrides: str | bool | None) -> Settings:
    """Command-line options win over the environment; options left unset do not."""
    given = {name: value for name, value in overrides.items() if value is not None}
    return Settings(**given)


def _read_diff(path: Path) -> list[ChangedFile]:
    # utf-8-sig drops the byte order mark that some Windows editors add.
    return parse_diff(path.read_bytes().decode("utf-8-sig", errors="replace"))


def _enable_debug_log() -> None:
    handler = logging.FileHandler(DEBUG_LOG, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logger = logging.getLogger("patchpilot")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
