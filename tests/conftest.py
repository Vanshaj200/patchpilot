"""Shared test helpers. The Claude API is replaced at the HTTP layer, so tests cost nothing."""

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from patchpilot.config import Settings
from patchpilot.llm import LLMClient

DIFF = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@ def total(items):
 def total(items):
-    return sum(items)
+    return sum(items[1:])
"""

COMMENT = {"file": "calc.py", "line": 2, "severity": "high", "message": "Skips the first item."}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep real API keys, settings and .env files away from every test."""
    for name in list(os.environ):
        if name.startswith(("ANTHROPIC_", "PATCHPILOT_")):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    yield
    logger = logging.getLogger("patchpilot")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)


def reply(
    payload: object = None,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    usage: dict[str, object] | None = None,
) -> httpx2.Response:
    """Build a Messages API response whose text block is `payload` as JSON."""
    text = payload if isinstance(payload, str) else json.dumps(payload or {"comments": []})
    body = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 100, "output_tokens": 20},
    }
    return httpx2.Response(200, json=body, headers={"request-id": "req_test"})


class FakeAPI:
    """Answers requests with the queued responses; the last one repeats."""

    def __init__(self) -> None:
        self.responses: list[httpx2.Response | Exception] = [reply()]
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        index = min(len(self.requests), len(self.responses)) - 1
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        return response

    def http_client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))

    def llm(self, settings: Settings | None = None) -> LLMClient:
        settings = settings or Settings(ANTHROPIC_API_KEY="test-key")
        return LLMClient(settings, http_client=self.http_client())

    def sent_body(self, index: int = 0) -> dict[str, object]:
        return json.loads(self.requests[index].content)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()
