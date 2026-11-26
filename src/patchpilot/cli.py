"""Command-line interface."""

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from patchpilot.config import Settings
from patchpilot.diff import ChangedFile, DiffParseError, parse_diff
from patchpilot.gentests import generate_tests
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import review_diff
from patchpilot.runner import Verdict, judge, run_tests
from patchpilot.store import SummaryRow, UsageStore

DEBUG_LOG = Path("patchpilot-debug.log")

# Local variables stay out of tracebacks because they can hold source code or the API key.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)

DiffFile = Annotated[
    Path,
    typer.Argument(exists=True, dir_okay=False, help="A file holding the output of `git diff`."),
]
RepoOption = Annotated[
    Path,
    typer.Option(exists=True, file_okay=False, help="Checkout that already contains the change."),
]
PromptOption = Annotated[str | None, typer.Option(help="Prompt version: v1 or v2.")]
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


@app.callback()
def main() -> None:
    """Review a git diff with the Claude API and test the changed code."""


@app.command()
def review(
    diff_file: DiffFile,
    repo: RepoOption = Path("."),
    prompt_version: PromptOption = None,
    routing: RoutingOption = None,
    prompt_cache: PromptCacheOption = None,
    response_cache: ResponseCacheOption = None,
    debug: DebugOption = False,
) -> None:
    """Print review comments for a diff as JSON."""
    if debug:
        _enable_debug_log()
    with _report_errors():
        settings = _load_settings(
            prompt_version=prompt_version,
            routing=routing,
            prompt_cache=prompt_cache,
            response_cache=response_cache,
        )
        files = _read_diff(diff_file)
        result = review_diff(files, _client(settings), settings, repo)
    # json.dumps escapes non-ASCII text, so output is safe on any console encoding.
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@app.command()
def gentests(
    diff_file: DiffFile,
    repo: RepoOption = Path("."),
    output: Annotated[Path, typer.Option(help="Where to write the tests.")] = Path(
        "patchpilot_tests.py"
    ),
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


@contextmanager
def _report_errors() -> Iterator[None]:
    """Turn expected failures into a short message and exit code 1."""
    try:
        yield
    except (DiffParseError, LLMError, ValidationError, sqlite3.Error, OSError) as error:
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
        table.append([row.feature, row.model, *map(str, counts), f"{row.cost_usd:.6f}"])
    totals = [sum(int(line[column]) for line in table[1:]) for column in range(2, len(header))]
    total_cost = sum(row.cost_usd for row in rows)
    table.append(["Total", "", *map(str, totals), f"{total_cost:.6f}"])

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
    unknown = sum(row.unknown_cost_requests for row in rows)
    if unknown:
        lines.append(f"{unknown} requests have no known cost and are not in the cost figures.")
    return "\n".join(lines)


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
