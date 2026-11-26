"""Find the changed Python functions and ask Claude to write pytest tests for them."""

import ast
import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, field_validator

from patchpilot.cost import estimate_tokens
from patchpilot.diff import ChangedFile, LineKind, read_new_version
from patchpilot.llm import LLMClient
from patchpilot.prompts import GENTESTS_PROMPTS, TestTarget, render_targets

logger = logging.getLogger(__name__)

_Function = ast.FunctionDef | ast.AsyncFunctionDef


class GeneratedTests(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_code: str

    @field_validator("test_code")
    @classmethod
    def must_be_a_python_module_with_tests(cls, code: str) -> str:
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            raise ValueError("test_code is not valid Python") from error
        if not any(
            isinstance(node, _Function) and node.name.startswith("test") for node in ast.walk(tree)
        ):
            raise ValueError("test_code contains no test functions")
        return code


def generate_tests(
    files: Sequence[ChangedFile], repo: Path, llm: LLMClient, prompt_version: str
) -> str | None:
    """Return a pytest module for the changed functions, or None if there are none."""
    targets: list[TestTarget] = []
    full_texts: list[str] = []
    for changed in files:
        source = read_new_version(changed, repo)
        if source is None:
            continue
        target = find_target(changed, source)
        if target is not None:
            targets.append(target)
            full_texts.append(source)
    if not targets:
        return None

    prompt = render_targets(targets)
    tokens_saved = estimate_tokens("\n".join(full_texts)) - estimate_tokens(prompt)
    generated = llm.complete(
        system=GENTESTS_PROMPTS[prompt_version],
        user=prompt,
        schema=GeneratedTests,
        feature="gentests",
        tokens_saved=tokens_saved,
    )
    return generated.test_code


def find_target(changed: ChangedFile, source: str) -> TestTarget | None:
    """Pick the top-level functions and methods that contain a change, with minimal context."""
    module = module_name(changed.path)
    if module is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.warning("%s is not valid Python, so no tests are generated for it", changed.path)
        return None

    lines = source.replace("\r\n", "\n").split("\n")
    positions = _changed_positions(changed)
    snippets: list[str] = []
    for node in tree.body:
        if isinstance(node, _Function) and _contains_change(node, positions):
            snippets.append(_source_of(node, lines))
        elif isinstance(node, ast.ClassDef):
            methods = [child for child in node.body if isinstance(child, _Function)]
            touched = [method for method in methods if _contains_change(method, positions)]
            if touched:
                # The constructor shows the model how to build the object.
                shown = [m for m in methods if m.name == "__init__" or m in touched]
                header = "\n".join(lines[node.lineno - 1 : node.body[0].lineno - 1])
                header = header or f"class {node.name}:"
                snippets.append("\n".join([header, *(_source_of(m, lines) for m in shown)]))
    if not snippets:
        return None

    imports = [
        _source_of(node, lines)
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    return TestTarget(changed.path, module, tuple(imports), tuple(snippets))


def module_name(path: str) -> str | None:
    """Turn "src/pkg/mod.py" into "pkg.mod". None for tests and files that cannot be imported."""
    pure = PurePosixPath(path)
    if pure.suffix != ".py" or pure.name.startswith("test_") or "tests" in pure.parts:
        return None
    parts = list(pure.with_suffix("").parts)
    if parts[0] == "src":
        del parts[0]
    if parts and parts[-1] == "__init__":
        del parts[-1]
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _changed_positions(changed: ChangedFile) -> set[int]:
    """Line numbers in the new file where something was added or removed."""
    positions: set[int] = set()
    for hunk in changed.hunks:
        # With no new lines, the header's start already names the line before the removal.
        previous = hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1
        for line in hunk.lines:
            if line.kind == LineKind.REMOVED:
                # A removed line has no new number: mark the line just before it.
                positions.add(previous)
            elif line.new_lineno is not None:
                previous = line.new_lineno
                if line.kind == LineKind.ADDED:
                    positions.add(previous)
    return positions


def _first_line(node: ast.stmt) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(decorator.lineno for decorator in decorators)])


def _contains_change(node: ast.stmt, positions: set[int]) -> bool:
    end = node.end_lineno or node.lineno
    return any(_first_line(node) <= position <= end for position in positions)


def _source_of(node: ast.stmt, lines: list[str]) -> str:
    return "\n".join(lines[_first_line(node) - 1 : node.end_lineno])
