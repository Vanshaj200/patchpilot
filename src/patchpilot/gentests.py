"""Find the changed Python functions and ask Claude to write pytest tests for them."""

import ast
import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, field_validator

from patchpilot.config import Settings
from patchpilot.cost import estimate_tokens
from patchpilot.diff import ChangedFile, LineKind, read_new_version
from patchpilot.llm import LLMClient
from patchpilot.prompts import GENTESTS_PROMPTS, TestTarget, render_targets
from patchpilot.router import choose_model

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
    files: Sequence[ChangedFile], repo: Path, llm: LLMClient, settings: Settings
) -> str | None:
    """Return a pytest module for the changed functions, or None if there are none."""
    targets: list[TestTarget] = []
    target_files: list[ChangedFile] = []
    full_texts: list[str] = []
    for changed in files:
        source = read_new_version(changed, repo)
        if source is None:
            continue
        target = find_target(changed, source)
        if target is not None:
            targets.append(target)
            target_files.append(changed)
            full_texts.append(source)
    if not targets:
        return None

    prompt = render_targets(targets)
    tokens_saved = estimate_tokens("\n".join(full_texts)) - estimate_tokens(prompt)
    generated = llm.complete(
        system=GENTESTS_PROMPTS[settings.prompt_version],
        user=prompt,
        schema=GeneratedTests,
        feature="gentests",
        model=choose_model(target_files, settings).model,
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
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, _Function) and _contains_change(node, positions):
            snippets.append(_source_of(node, lines))
            selected.append(node)
        elif isinstance(node, ast.ClassDef):
            methods = [child for child in node.body if isinstance(child, _Function)]
            touched = [method for method in methods if _contains_change(method, positions)]
            if touched:
                # Fields and the constructor show the model how to build the object.
                shown = [
                    member
                    for member in node.body
                    if isinstance(member, ast.Assign | ast.AnnAssign)
                    or member in touched
                    or (isinstance(member, _Function) and member.name == "__init__")
                ]
                # Decorators such as @dataclass and the "class" line come before the body.
                header = lines[_first_line(node) - 1 : _first_line(node.body[0]) - 1]
                header = header or [f"class {node.name}:"]
                snippets.append("\n".join([*header, *(_source_of(m, lines) for m in shown)]))
                selected += shown
    if not snippets:
        return None

    context = [_source_of(node, lines) for node in _context_statements(tree, selected)]
    return TestTarget(changed.path, module, tuple(context), tuple(snippets))


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


def _context_statements(tree: ast.Module, selected: list[ast.stmt]) -> list[ast.stmt]:
    """Imports, and the module-level values that the selected functions mention."""
    used = {part.id for node in selected for part in ast.walk(node) if isinstance(part, ast.Name)}
    context: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            context.append(node)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in used for target in targets):
                context.append(node)
    return context


def _first_line(node: ast.stmt) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(decorator.lineno for decorator in decorators)])


def _contains_change(node: ast.stmt, positions: set[int]) -> bool:
    end = node.end_lineno or node.lineno
    return any(_first_line(node) <= position <= end for position in positions)


def _source_of(node: ast.stmt, lines: list[str]) -> str:
    return "\n".join(lines[_first_line(node) - 1 : node.end_lineno])
