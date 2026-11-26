"""Turn a parsed diff into review comments. Shared by the CLI and the eval harness."""

import logging
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.diff import ChangedFile
from patchpilot.llm import LLMClient
from patchpilot.prompts import REVIEW_PROMPTS, render_diff

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


def review_diff(files: Sequence[ChangedFile], llm: LLMClient, prompt_version: str) -> Review:
    """Ask Claude to review the changed text files. No request is sent if there are none."""
    reviewable = [changed for changed in files if changed.hunks and not changed.is_binary]
    if not reviewable:
        return Review(comments=[])

    review = llm.complete(
        system=REVIEW_PROMPTS[prompt_version], user=render_diff(reviewable), schema=Review
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
