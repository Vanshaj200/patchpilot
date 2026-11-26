"""Parse the output of `git diff` into changed files, hunks and lines."""

import re
from dataclasses import dataclass
from enum import StrEnum

# Example: "@@ -22,7 +22,8 @@ def main():". A missing count means 1.
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")


class ChangeType(StrEnum):
    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"


class LineKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class Line:
    """One line inside a hunk. A line number is None if the line is absent on that side."""

    kind: LineKind
    content: str
    old_lineno: int | None
    new_lineno: int | None


@dataclass(frozen=True)
class Hunk:
    """One "@@" block: a run of changed lines with their surrounding context."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class ChangedFile:
    """One file in the diff. A path is None if the file does not exist on that side."""

    change_type: ChangeType
    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]


class DiffParseError(ValueError):
    """The text is not a git diff that PatchPilot understands.

    Messages give a line number but never quote the line, so source code cannot leak into logs.
    """

    def __init__(self, message: str, line_number: int) -> None:
        super().__init__(f"line {line_number}: {message}")
        self.line_number = line_number


def parse_diff(text: str) -> list[ChangedFile]:
    """Parse the full output of `git diff`. Empty input gives an empty list."""
    # Not str.splitlines(): it also splits on form feeds and other characters found in code.
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()

    files: list[ChangedFile] = []
    index = 0
    while index < len(lines):
        changed_file, index = _parse_file(lines, index)
        files.append(changed_file)
    return files


def _parse_file(lines: list[str], start: int) -> tuple[ChangedFile, int]:
    """Parse one file section. Return it with the index of the first line after it."""
    if not lines[start].startswith("diff --git "):
        raise DiffParseError("expected a 'diff --git' header", start + 1)

    change_type = ChangeType.MODIFIED
    old_path: str | None = None
    new_path: str | None = None

    index = start + 1
    while index < len(lines) and not lines[index].startswith(("diff --git ", "@@")):
        line = lines[index].rstrip("\r")
        if line.startswith("new file mode "):
            change_type = ChangeType.ADDED
        elif line.startswith("deleted file mode "):
            change_type = ChangeType.DELETED
        elif line.startswith("--- "):
            old_path = _marker_path(line.removeprefix("--- "), "a/", index + 1)
        elif line.startswith("+++ "):
            new_path = _marker_path(line.removeprefix("+++ "), "b/", index + 1)
        elif not line.startswith("index "):
            raise DiffParseError("unsupported line in file header", index + 1)
        index += 1

    hunks: list[Hunk] = []
    while index < len(lines) and lines[index].startswith("@@"):
        hunk, index = _parse_hunk(lines, index)
        hunks.append(hunk)

    if not hunks:
        raise DiffParseError("file section has no hunks; the diff may be truncated", start + 1)
    if change_type != ChangeType.ADDED and old_path is None:
        raise DiffParseError("file section is missing the old path", start + 1)
    if change_type != ChangeType.DELETED and new_path is None:
        raise DiffParseError("file section is missing the new path", start + 1)

    return ChangedFile(change_type, old_path, new_path, tuple(hunks)), index


def _marker_path(value: str, prefix: str, line_number: int) -> str | None:
    """Read the path from a "---" or "+++" line."""
    if value == "/dev/null":
        return None
    if not value.startswith(prefix):
        raise DiffParseError(f"expected a path starting with '{prefix}'", line_number)
    return value.removeprefix(prefix)


def _parse_hunk(lines: list[str], start: int) -> tuple[Hunk, int]:
    """Parse one hunk, using the counts in its header to find where it ends."""
    match = _HUNK_HEADER.match(lines[start].rstrip("\r"))
    if match is None:
        raise DiffParseError("malformed hunk header", start + 1)
    old_start, new_start = int(match[1]), int(match[3])
    old_count = int(match[2] or 1)
    new_count = int(match[4] or 1)

    body: list[Line] = []
    old_lineno, new_lineno = old_start, new_start
    old_left, new_left = old_count, new_count
    index = start + 1
    # Counting is needed because a removed line "-- x" looks like a "--- x" file header.
    while old_left > 0 or new_left > 0:
        if index == len(lines):
            raise DiffParseError("hunk ends before its line counts are reached", index)
        marker, content = lines[index][:1], lines[index][1:]
        if marker == " ":
            body.append(Line(LineKind.CONTEXT, content, old_lineno, new_lineno))
            old_lineno, new_lineno = old_lineno + 1, new_lineno + 1
            old_left, new_left = old_left - 1, new_left - 1
        elif marker == "-":
            body.append(Line(LineKind.REMOVED, content, old_lineno, None))
            old_lineno, old_left = old_lineno + 1, old_left - 1
        elif marker == "+":
            body.append(Line(LineKind.ADDED, content, None, new_lineno))
            new_lineno, new_left = new_lineno + 1, new_left - 1
        elif marker != "\\":  # "\ No newline at end of file" is a note, not a line of code.
            raise DiffParseError("unexpected line inside a hunk", index + 1)
        index += 1

    if old_left or new_left:
        raise DiffParseError("hunk has more lines than its header declares", start + 1)
    if index < len(lines) and lines[index].startswith("\\"):
        index += 1

    section = match[5] or ""
    return Hunk(old_start, old_count, new_start, new_count, section, tuple(body)), index
