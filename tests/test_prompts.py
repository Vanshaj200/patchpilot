from conftest import DIFF

from patchpilot.diff import parse_diff
from patchpilot.prompts import REVIEW_PROMPTS, render_diff


def test_both_prompt_versions_exist() -> None:
    assert set(REVIEW_PROMPTS) == {"v1", "v2"}
    assert REVIEW_PROMPTS["v1"] != REVIEW_PROMPTS["v2"]


def test_rendered_diff_numbers_new_file_lines() -> None:
    assert render_diff(parse_diff(DIFF)).split("\n") == [
        "File: calc.py (modified)",
        "@@ def total(items):",
        "    1  def total(items):",
        "      -    return sum(items)",
        "    2 +    return sum(items[1:])",
    ]


def test_carriage_returns_are_not_sent() -> None:
    assert "\r" not in render_diff(parse_diff(DIFF.replace("\n", "\r\n")))


def test_deleted_file_code_is_not_sent() -> None:
    text = (
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n"
        "--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-secret = 1\n"
    )

    assert render_diff(parse_diff(text)) == "File: old.py (deleted)"
