from pathlib import Path

import pytest

from patchpilot.diff import ChangedFile, ChangeType, DiffParseError, Line, LineKind, parse_diff

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    # read_bytes, not read_text: read_text would turn CRLF inside the diff into LF.
    return (FIXTURES / f"{name}.diff").read_bytes().decode("utf-8")


def parse_one(name: str) -> ChangedFile:
    files = parse_diff(load(name))
    assert len(files) == 1
    return files[0]


def test_empty_input_gives_no_files() -> None:
    assert parse_diff("") == []


def test_modified_file_with_multiple_hunks() -> None:
    changed = parse_one("modified_multiple_hunks")

    assert changed.change_type == ChangeType.MODIFIED
    assert changed.old_path == changed.new_path == "app.py"
    first, second = changed.hunks
    assert (first.old_start, first.old_count, first.new_start, first.new_count) == (1, 5, 1, 5)
    assert (second.old_start, second.old_count, second.new_start, second.new_count) == (
        22,
        7,
        22,
        8,
    )
    assert second.section == "value_21 = 21"


def test_line_numbers_advance_independently_on_each_side() -> None:
    second = parse_one("modified_multiple_hunks").hunks[1]

    assert second.lines[3:7] == (
        Line(LineKind.REMOVED, "value_25 = 25", 25, None),
        Line(LineKind.ADDED, "value_25 = 250", None, 25),
        Line(LineKind.ADDED, "extra = True", None, 26),
        Line(LineKind.CONTEXT, "value_26 = 26", 26, 27),
    )


def test_added_file_has_no_old_path() -> None:
    changed = parse_one("added_file")

    assert changed.change_type == ChangeType.ADDED
    assert (changed.old_path, changed.new_path) == (None, "new_module.py")
    assert [line.new_lineno for line in changed.hunks[0].lines] == [1, 2]


def test_deleted_file_has_no_new_path() -> None:
    changed = parse_one("deleted_file")

    assert changed.change_type == ChangeType.DELETED
    assert (changed.old_path, changed.new_path) == ("old_module.py", None)
    assert {line.kind for line in changed.hunks[0].lines} == {LineKind.REMOVED}


def test_multiple_files_are_kept_in_order() -> None:
    files = parse_diff(load("multiple_files"))

    assert [(f.change_type, f.old_path, f.new_path) for f in files] == [
        (ChangeType.MODIFIED, "a.py", "a.py"),
        (ChangeType.DELETED, "b.py", None),
        (ChangeType.ADDED, None, "c.py"),
    ]


def test_header_without_counts_means_one_line() -> None:
    hunk = parse_diff(load("multiple_files"))[0].hunks[0]

    assert (hunk.old_count, hunk.new_count) == (1, 1)


def test_no_newline_note_is_not_a_line() -> None:
    lines = parse_one("no_newline_at_eof").hunks[0].lines

    assert [line.content for line in lines] == ["a = 1", "b = 2", "b = 3"]


def test_crlf_stays_part_of_the_content() -> None:
    lines = parse_one("crlf_content").hunks[0].lines

    assert [line.content for line in lines] == ["a = 1\r", "b = 2\r", "b = 3\r"]


def test_code_that_looks_like_a_file_header_stays_in_the_hunk() -> None:
    lines = parse_one("dash_lines").hunks[0].lines

    assert [(line.kind, line.content) for line in lines[1:]] == [
        (LineKind.REMOVED, "-- note"),
        (LineKind.ADDED, "++ counter"),
    ]


def test_form_feed_inside_code_does_not_split_the_line() -> None:
    text = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\x0cb\n+c\n"

    assert parse_diff(text)[0].hunks[0].lines[0].content == "a\x0cb"


HEADER = "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("hello\n", "line 1: expected a 'diff --git' header"),
        (HEADER, "no hunks"),
        (HEADER + "@@ bad @@\n", "line 4: malformed hunk header"),
        (HEADER + "@@ -1,2 +1,2 @@\n a\n", "ends before its line counts"),
        (HEADER + "@@ -1 +1,2 @@\n a\n b\n", "more lines than its header"),
        (HEADER + "@@ -1,2 +1,2 @@\n a\nnonsense\n", "line 6: unexpected line inside a hunk"),
        (HEADER + "@@ -1 +1 @@\n a\nextra\n", "expected a 'diff --git' header"),
        ("diff --git a/f b/f\nsurprise\n", "unsupported line in file header"),
        ("diff --git f f\n--- f\n+++ f\n@@ -1 +1 @@\n a\n", "path starting with 'a/'"),
        ("diff --git a/f b/f\n+++ b/f\n@@ -1 +1 @@\n a\n", "missing the old path"),
        ("diff --git a/f b/f\n--- a/f\n@@ -1 +1 @@\n a\n", "missing the new path"),
    ],
)
def test_bad_input_raises(text: str, message: str) -> None:
    with pytest.raises(DiffParseError, match=message):
        parse_diff(text)


def test_error_messages_do_not_quote_source_code() -> None:
    with pytest.raises(DiffParseError) as caught:
        parse_diff(HEADER + "@@ -1,2 +1,2 @@\n a\nsecret_token = 'abc'\n")

    assert "secret_token" not in str(caught.value)
    assert caught.value.line_number == 6
