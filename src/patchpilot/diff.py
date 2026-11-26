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
    RENAMED = "renamed"


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
    is_binary: bool


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
    header = lines[start].rstrip("\r")
    if header.startswith(("diff --cc ", "diff --combined ")):
        raise DiffParseError("combined merge diffs are not supported", start + 1)
    if not header.startswith("diff --git "):
        raise DiffParseError("expected a 'diff --git' header", start + 1)

    change_type = ChangeType.MODIFIED
    old_path: str | None = None
    new_path: str | None = None
    has_markers = mode_changed = is_binary = False

    index = start + 1
    while index < len(lines) and not lines[index].startswith(("diff --", "@@")):
        line = lines[index].rstrip("\r")
        if line.startswith("new file mode "):
            change_type = ChangeType.ADDED
        elif line.startswith("deleted file mode "):
            change_type = ChangeType.DELETED
        elif line.startswith("rename from "):
            change_type = ChangeType.RENAMED
            old_path = _unquote(line.removeprefix("rename from "))
        elif line.startswith("rename to "):
            new_path = _unquote(line.removeprefix("rename to "))
        elif line.startswith("--- "):
            has_markers = True
            old_path = _marker_path(line.removeprefix("--- "), "a/", index + 1)
        elif line.startswith("+++ "):
            new_path = _marker_path(line.removeprefix("+++ "), "b/", index + 1)
        elif line.startswith("new mode "):
            mode_changed = True
        elif line.startswith("Binary files "):
            is_binary = True
        elif line == "GIT binary patch":
            is_binary = True
            # Skip the encoded data: it is of no use to a reviewer.
            while index + 1 < len(lines) and not lines[index + 1].startswith("diff --"):
                index += 1
        elif line.startswith(("copy from ", "copy to ")):
            raise DiffParseError("diffs with copy detection are not supported", index + 1)
        elif not line.startswith(
            ("index ", "old mode ", "similarity index ", "dissimilarity index ")
        ):
            raise DiffParseError("unsupported line in file header", index + 1)
        index += 1

    hunks: list[Hunk] = []
    while index < len(lines) and lines[index].startswith("@@"):
        hunk, index = _parse_hunk(lines, index)
        hunks.append(hunk)

    # Empty files, pure renames, mode changes and binary files can legitimately have no hunks.
    unexplained = change_type == ChangeType.MODIFIED and not (mode_changed or is_binary)
    if not hunks and (has_markers or unexplained):
        raise DiffParseError("file section has no hunks; the diff may be truncated", start + 1)

    if old_path is None and new_path is None:
        shared_path = _header_path(header, start + 1)
        old_path = None if change_type == ChangeType.ADDED else shared_path
        new_path = None if change_type == ChangeType.DELETED else shared_path
    if change_type != ChangeType.ADDED and old_path is None:
        raise DiffParseError("file section is missing the old path", start + 1)
    if change_type != ChangeType.DELETED and new_path is None:
        raise DiffParseError("file section is missing the new path", start + 1)

    return ChangedFile(change_type, old_path, new_path, tuple(hunks), is_binary), index


def _marker_path(value: str, prefix: str, line_number: int) -> str | None:
    """Read the path from a "---" or "+++" line."""
    # Git ends these lines with a tab when the path contains a space.
    value = value.removesuffix("\t")
    if value == "/dev/null":
        return None
    return _strip_prefix(value, prefix, line_number)


def _header_path(header: str, line_number: int) -> str:
    """Read the path from "diff --git a/P b/P", for sections where no other line names the file.

    Both halves name the same file, so they are equally long. Splitting in the middle therefore
    works even when the path contains spaces.
    """
    paths = header.removeprefix("diff --git ")
    middle = len(paths) // 2
    old_path = _strip_prefix(paths[:middle], "a/", line_number)
    new_path = _strip_prefix(paths[middle + 1 :], "b/", line_number)
    if paths[middle : middle + 1] != " " or old_path != new_path:
        raise DiffParseError("cannot read the paths in the 'diff --git' header", line_number)
    return new_path


def _strip_prefix(path: str, prefix: str, line_number: int) -> str:
    path = _unquote(path)
    if not path.startswith(prefix):
        raise DiffParseError(f"expected a path starting with '{prefix}'", line_number)
    return path.removeprefix(prefix)


def _unquote(path: str) -> str:
    """Undo git's quoting of unusual file names, such as "caf\\303\\251.py"."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    # unicode_escape yields one character per escaped byte; latin-1 turns those back into bytes.
    raw = path[1:-1].encode("utf-8").decode("unicode_escape").encode("latin-1")
    return raw.decode("utf-8", errors="surrogateescape")


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
