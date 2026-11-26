"""Command-line interface."""

import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from patchpilot.config import Settings
from patchpilot.diff import ChangedFile, DiffParseError, parse_diff
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import review_diff
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
DebugOption = Annotated[
    bool,
    typer.Option("--debug", help=f"Write prompts and replies, including code, to {DEBUG_LOG}."),
]


@app.callback()
def main() -> None:
    """Review a git diff with the Claude API."""


@app.command()
def review(
    diff_file: DiffFile,
    repo: RepoOption = Path("."),
    prompt_version: Annotated[str | None, typer.Option(help="Review prompt: v1 or v2.")] = None,
    debug: DebugOption = False,
) -> None:
    """Print review comments for a diff as JSON."""
    if debug:
        _enable_debug_log()
    try:
        settings = _load_settings(prompt_version=prompt_version)
        files = _read_diff(diff_file)
        llm = LLMClient(settings, on_request=UsageStore(settings.db_path).add)
        result = review_diff(files, llm, settings.prompt_version, repo)
    except (DiffParseError, LLMError, ValidationError, sqlite3.Error) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    # json.dumps escapes non-ASCII text, so output is safe on any console encoding.
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@app.command()
def report() -> None:
    """Print total tokens and cost by feature and model."""
    try:
        settings = _load_settings()
        rows = UsageStore(settings.db_path).summary()
    except (ValidationError, sqlite3.Error) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(_format_report(rows) if rows else f"No requests logged in {settings.db_path}.")


def _format_report(rows: Sequence[SummaryRow]) -> str:
    header = ["Feature", "Model", "Requests", "Input", "Output", "Cache write", "Cache read"]
    table = [[*header, "Cost (USD)"]]
    for row in rows:
        counts = [row.requests, row.input_tokens, row.output_tokens]
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
    lines.append(f"\nEstimated input tokens saved by sending only hunks: {saved}")
    unknown = sum(row.unknown_cost_requests for row in rows)
    if unknown:
        lines.append(f"{unknown} requests have no known cost and are not in the cost figures.")
    return "\n".join(lines)


def _load_settings(**overrides: str | None) -> Settings:
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
