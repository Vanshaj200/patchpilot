"""Run a generated test module against a copy of the code, with a time limit.

This protects the checkout and hides secrets held in environment variables. It is NOT a security
sandbox: the tests can still use the network and read any file your user account can read.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Hidden files (such as .env and .git) and bulky or private folders are not copied.
_NOT_COPIED = shutil.ignore_patterns(
    ".*", "venv", "env", "node_modules", "__pycache__", "*.db", "*.log"
)
# An allow-list: anything not named here, such as ANTHROPIC_API_KEY, never reaches the tests.
_PASSED_VARIABLES = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
_OUTPUT_LIMIT = 4000


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class Verdict(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    POSSIBLE_BUG = "possible bug"
    BAD_TEST = "bad test"
    UNVERIFIED = "unverified"
    SKIPPED = "skipped"


_BROKEN = (Outcome.FAILED, Outcome.ERROR)


@dataclass(frozen=True)
class CaseResult:
    name: str
    outcome: Outcome
    message: str


@dataclass(frozen=True)
class RunResult:
    cases: tuple[CaseResult, ...]
    timed_out: bool
    output: str


def run_tests(test_code: str, source_dir: Path, timeout_seconds: float) -> RunResult:
    """Copy `source_dir` to a temporary folder, add the tests there and run pytest."""
    with tempfile.TemporaryDirectory(prefix="patchpilot-", ignore_cleanup_errors=True) as temp:
        work = Path(temp) / "work"
        shutil.copytree(source_dir, work, ignore=_NOT_COPIED, symlinks=True)
        test_dir = work / "_patchpilot_tests"
        test_dir.mkdir()
        test_file = test_dir / "test_generated.py"
        test_file.write_text(test_code, encoding="utf-8")
        config = test_dir / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        report = Path(temp) / "report.xml"
        log = Path(temp) / "output.log"

        # Our own empty config and no conftest files, so project settings cannot change the run.
        command = [sys.executable, "-m", "pytest", str(test_file), "-q", "--noconftest"]
        command += ["-c", str(config), f"--rootdir={work}", f"--junitxml={report}"]
        command += ["-p", "no:cacheprovider"]
        environment = {name: os.environ[name] for name in _PASSED_VARIABLES if name in os.environ}
        environment |= {
            "PYTHONPATH": os.pathsep.join([str(work), str(work / "src")]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        # Output goes to a file: with a pipe, a leftover child process could block us forever.
        with log.open("wb") as output:
            try:
                subprocess.run(
                    command,
                    cwd=work,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_seconds,
                    check=False,
                )
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True

        text = log.read_text(encoding="utf-8", errors="replace")[-_OUTPUT_LIMIT:]
        cases = () if timed_out else _read_report(report)
        return RunResult(cases, timed_out, text)


def judge(target: RunResult, known_good: RunResult | None) -> list[tuple[CaseResult, Verdict]]:
    """Decide what each result on the target code means.

    A test that fails on code known to be correct is wrong itself. A test that passes there but
    fails on the target code points to a bug.
    """
    good = {} if known_good is None else {case.name: case for case in known_good.cases}
    judged: list[tuple[CaseResult, Verdict]] = []
    for case in target.cases:
        reference = good.get(case.name)
        if reference is not None and reference.outcome in _BROKEN:
            verdict = Verdict.BAD_TEST
        elif case.outcome == Outcome.SKIPPED:
            verdict = Verdict.SKIPPED
        elif known_good is not None and reference is None:
            verdict = Verdict.UNVERIFIED
        elif case.outcome == Outcome.PASSED:
            verdict = Verdict.PASSED
        else:
            verdict = Verdict.FAILED if known_good is None else Verdict.POSSIBLE_BUG
        judged.append((case, verdict))
    return judged


def _read_report(report: Path) -> tuple[CaseResult, ...]:
    """Read pytest's JUnit XML report. A missing report means pytest never finished."""
    if not report.is_file():
        return ()
    cases: dict[str, CaseResult] = {}
    for element in ElementTree.parse(report).iter("testcase"):
        parts = (element.get("classname", ""), element.get("name", ""))
        name = "::".join(part for part in parts if part).removeprefix(
            "_patchpilot_tests.test_generated::"
        )
        outcome, message = Outcome.PASSED, ""
        for tag, found in (
            ("failure", Outcome.FAILED),
            ("error", Outcome.ERROR),
            ("skipped", Outcome.SKIPPED),
        ):
            child = element.find(tag)
            if child is not None:
                outcome, message = found, child.get("message", "")
                break
        # A test can appear twice (it fails, then its cleanup fails): keep the bad news.
        if name not in cases or outcome in _BROKEN:
            cases[name] = CaseResult(name, outcome, message)
    return tuple(cases.values())
