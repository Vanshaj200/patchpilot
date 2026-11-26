"""Command-line interface."""

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from patchpilot.config import Settings
from patchpilot.diff import ChangedFile, DiffParseError, parse_diff
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import review_diff

DEBUG_LOG = Path("patchpilot-debug.log")

# Local variables stay out of tracebacks because they can hold source code or the API key.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)

DiffFile = Annotated[
    Path,
    typer.Argument(exists=True, dir_okay=False, help="A file holding the output of `git diff`."),
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
    prompt_version: Annotated[str | None, typer.Option(help="Review prompt: v1 or v2.")] = None,
    debug: DebugOption = False,
) -> None:
    """Print review comments for a diff as JSON."""
    if debug:
        _enable_debug_log()
    try:
        settings = _load_settings(prompt_version=prompt_version)
        files = _read_diff(diff_file)
        result = review_diff(files, LLMClient(settings), settings.prompt_version)
    except (DiffParseError, LLMError, ValidationError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    # json.dumps escapes non-ASCII text, so output is safe on any console encoding.
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


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
