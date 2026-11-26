"""Turn a parsed diff into review comments. Shared by the CLI and the eval harness."""

import logging
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.config import Settings
from patchpilot.cost import estimate_tokens
from patchpilot.diff import ChangedFile, read_new_version
from patchpilot.llm import LLMClient
from patchpilot.prompts import REVIEW_PROMPTS, render_diff
from patchpilot.router import choose_model

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line: int = Field(ge=1)
    severity: Severity
    message: str = Field(min_length=1)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comments: list[ReviewComment]


def github_annotations(review: Review) -> str:
    """Format comments as GitHub Actions commands, which appear on the pull request's lines."""
    levels = {Severity.HIGH: "error", Severity.MEDIUM: "warning", Severity.LOW: "notice"}
    return "\n".join(
        f"::{levels[comment.severity]} file={_escape_property(comment.file)},"
        f"line={comment.line},title=PatchPilot ({comment.severity})::"
        f"{_escape_data(comment.message)}"
        for comment in review.comments
    )


def _escape_data(text: str) -> str:
    # The message comes from a model: a raw newline would let it start a command of its own.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def estimate_tokens_saved(files: Sequence[ChangedFile], repo: Path) -> int | None:
    """Estimate the tokens saved by sending hunks instead of whole files.

    Only files readable in `repo` are compared. The result is negative when the line numbers
    cost more than the skipped code, as for a new file. None means nothing could be compared.
    """
    full_texts: list[str] = []
    compared: list[ChangedFile] = []
    for changed in files:
        text = read_new_version(changed, repo)
        if text is not None:
            full_texts.append(text)
            compared.append(changed)
    if not compared:
        return None
    return estimate_tokens("\n".join(full_texts)) - estimate_tokens(render_diff(compared))


def review_diff(
    files: Sequence[ChangedFile], llm: LLMClient, settings: Settings, repo: Path
) -> Review:
    """Ask Claude to review the changed text files. No request is sent if there are none."""
    reviewable = [changed for changed in files if changed.hunks and not changed.is_binary]
    if not reviewable:
        return Review(comments=[])

    tokens_saved = estimate_tokens_saved(reviewable, repo)
    logger.info("estimated input tokens saved by sending only hunks: %s", tokens_saved)
    review = llm.complete(
        system=REVIEW_PROMPTS[settings.prompt_version],
        user=render_diff(reviewable),
        schema=Review,
        feature="review",
        model=choose_model(reviewable, settings).model,
        tokens_saved=tokens_saved,
    )

    # A valid JSON shape does not make the content true: drop comments on files we never sent.
    sent_paths = {changed.path for changed in reviewable}
    kept = [comment for comment in review.comments if comment.file in sent_paths]
    if len(kept) < len(review.comments):
        logger.warning(
            "dropped %d comments on files that are not in the diff",
            len(review.comments) - len(kept),
        )
    return Review(comments=kept)
