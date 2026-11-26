"""Runs inside the browser (Pyodide): run pytest on pasted code and report each test."""

import contextlib
import io
import json
import os
import sys

import pytest

_LIMIT = 4000


class Collector:
    """A pytest plugin that notes what happened to each test."""

    def __init__(self) -> None:
        self.results: dict[str, dict[str, str]] = {}

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A file that cannot even be imported has no tests, only this report.
        if report.failed:
            self._add(report.nodeid or "test file", "error", str(report.longrepr))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # Each test reports setup, call and teardown. Success counts only for the call.
        if report.passed:
            if report.when == "call":
                self._add(report.nodeid, "passed", "")
            return
        if report.skipped:
            self._add(report.nodeid, "skipped", "")
            return
        outcome = "failed" if report.when == "call" else "error"
        crash = getattr(report.longrepr, "reprcrash", None)
        self._add(report.nodeid, outcome, crash.message if crash else str(report.longrepr))

    def _add(self, node_id: str, outcome: str, message: str) -> None:
        name = node_id.split("::", 1)[-1]
        self.results[node_id] = {"name": name, "outcome": outcome, "message": message[-_LIMIT:]}


def run(code: str, tests: str, folder: str) -> str:
    """Save the code as snippet.py beside the tests, run them and return the results as JSON."""
    os.makedirs(folder, exist_ok=True)
    for name, text in (("snippet.py", code), ("test_snippet.py", tests), ("pytest.ini", "")):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    os.chdir(folder)
    sys.path.insert(0, folder)

    collector = Collector()
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        arguments = ["test_snippet.py", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini"]
        pytest.main(arguments, plugins=[collector])
    results = list(collector.results.values())
    return json.dumps({"results": results, "output": output.getvalue()[-_LIMIT:]})
