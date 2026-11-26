"""Choose a model with fixed rules: the small one only for changes that look trivial."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from patchpilot.config import Settings
from patchpilot.diff import ChangedFile, LineKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Route:
    model: str
    reason: str


def choose_model(files: Sequence[ChangedFile], settings: Settings) -> Route:
    """Apply the rules in order; any sign of complexity or risk selects the large model."""
    route = _match_rule(files, settings)
    logger.info("model %s: %s", route.model, route.reason)
    return route


def _match_rule(files: Sequence[ChangedFile], settings: Settings) -> Route:
    large = settings.model
    if not settings.routing:
        return Route(large, "routing is off")
    if len(files) > settings.route_max_files:
        return Route(large, f"more than {settings.route_max_files} changed files")
    changed = [
        line.content
        for changed_file in files
        for hunk in changed_file.hunks
        for line in hunk.lines
        if line.kind != LineKind.CONTEXT
    ]
    if len(changed) > settings.route_max_changed_lines:
        return Route(large, f"more than {settings.route_max_changed_lines} changed lines")
    # A one-line change to a query or a password check is small but not trivial.
    if any(settings.route_risky_pattern.search(content) for content in changed):
        return Route(large, "a changed line matches the risky pattern")
    return Route(settings.small_model, "small change with no risky lines")
