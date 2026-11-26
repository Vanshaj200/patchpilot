from pathlib import Path

import pytest
from conftest import FakeAPI, reply

from patchpilot.config import Settings
from patchpilot.diff import ChangedFile, parse_diff
from patchpilot.gentests import find_target, generate_tests, module_name
from patchpilot.llm import LLMError

SOURCE = """\
import math
from os import path


def unchanged():
    return 0


@staticmethod
def area(radius):
    return math.pi * radius**2


class Basket:
    def __init__(self):
        self.items = []

    def add(self, item):
        self.items.append(item)

    def clear(self):
        self.items = []
"""


def change(*hunk_lines: str, path: str = "shapes.py", start: int = 1) -> ChangedFile:
    old = sum(line[0] in " -" for line in hunk_lines)
    new = sum(line[0] in " +" for line in hunk_lines)
    body = "\n".join(hunk_lines)
    text = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{start},{old} +{start},{new} @@\n{body}\n"
    )
    return parse_diff(text)[0]


def snippets(changed: ChangedFile, source: str = SOURCE) -> str:
    target = find_target(changed, source)
    return "" if target is None else "\n".join(target.snippets)


def test_added_line_selects_its_function_with_decorator() -> None:
    selected = snippets(change("+    return math.pi * radius**2", start=11))

    assert selected.startswith("@staticmethod\ndef area(radius):")
    assert "unchanged" not in selected


def test_method_change_includes_class_and_constructor_only() -> None:
    selected = snippets(change("+        self.items.append(item)", start=19))

    assert selected.startswith("class Basket:")
    assert "def __init__" in selected
    assert "def add" in selected
    assert "def clear" not in selected


def test_removed_last_line_selects_the_function_before_it() -> None:
    selected = snippets(change("     return 0", "-    print('gone')", start=6))

    assert "def unchanged" in selected
    assert "def area" not in selected


def test_removal_without_context_lines() -> None:
    text = (
        "diff --git a/shapes.py b/shapes.py\n--- a/shapes.py\n+++ b/shapes.py\n"
        "@@ -7 +6,0 @@\n-    print('gone')\n"
    )

    assert "def unchanged" in snippets(parse_diff(text)[0])


def test_change_outside_any_function_selects_nothing() -> None:
    assert find_target(change("+import math", start=1), SOURCE) is None


def test_target_lists_imports_and_module() -> None:
    target = find_target(change("+    return 0", path="src/pkg/shapes.py", start=6), SOURCE)

    assert target is not None
    assert target.module == "pkg.shapes"
    assert target.context == ("import math", "from os import path")


def test_context_includes_only_the_values_that_are_used() -> None:
    source = (
        "LIMIT = 3\nRATE: float = 0.5\nUNUSED = 1\n\n\ndef f(x):\n    return x * RATE < LIMIT\n"
    )

    target = find_target(change("+    return x * RATE < LIMIT", start=7), source)

    assert target is not None
    assert target.context == ("LIMIT = 3", "RATE: float = 0.5")


def test_invalid_source_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    assert find_target(change("+def (", start=1), "def (\n") is None
    assert "not valid Python" in caplog.text


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app.py", "app"),
        ("src/pkg/mod.py", "pkg.mod"),
        ("pkg/__init__.py", "pkg"),
        ("README.md", None),
        ("tests/helpers.py", None),
        ("pkg/test_mod.py", None),
        ("my-scripts/run.py", None),
        ("__init__.py", None),
    ],
)
def test_module_name(path: str, expected: str | None) -> None:
    assert module_name(path) == expected


def test_generates_tests_from_the_changed_function_only(api: FakeAPI, tmp_path: Path) -> None:
    (tmp_path / "shapes.py").write_text(SOURCE)
    code = "def test_area() -> None:\n    assert True\n"
    api.responses = [reply({"test_code": code})]

    result = generate_tests(
        [change("+    return math.pi * radius**2", start=11)], tmp_path, api.llm(), Settings()
    )

    assert result == code
    sent = str(api.sent_body()["messages"])
    assert "Module: shapes" in sent
    assert "def unchanged" not in sent
    assert api.records[0].feature == "gentests"
    assert api.records[0].tokens_saved is not None


def test_no_request_without_changed_functions(api: FakeAPI, tmp_path: Path) -> None:
    changed = change("+import math", start=1)

    assert generate_tests([changed], tmp_path, api.llm(), Settings()) is None
    (tmp_path / "shapes.py").write_text(SOURCE)
    assert generate_tests([changed], tmp_path, api.llm(), Settings()) is None
    assert api.requests == []


@pytest.mark.parametrize("code", ["def test_(:", "x = 1\n", ""])
def test_unusable_test_code_is_rejected(api: FakeAPI, tmp_path: Path, code: str) -> None:
    (tmp_path / "shapes.py").write_text(SOURCE)
    api.responses = [reply({"test_code": code})]

    with pytest.raises(LLMError, match="did not match"):
        generate_tests(
            [change("+    return 0", start=6)], tmp_path, api.llm(), Settings(prompt_version="v1")
        )

    assert api.records[0].status == "invalid_reply"
