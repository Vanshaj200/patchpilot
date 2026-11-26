import logging

import httpx2
import pytest
from conftest import COMMENT, FakeAPI, reply

from patchpilot.config import Settings
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import Review


def ask(llm: LLMClient, user: str = "diff text") -> Review:
    return llm.complete(system="Review this.", user=user, schema=Review, feature="review")


def overloaded() -> httpx2.Response:
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
    return httpx2.Response(529, json=error, headers={"retry-after-ms": "1"})


def test_returns_validated_reply(api: FakeAPI) -> None:
    api.responses = [reply({"comments": [COMMENT]})]

    result = ask(api.llm())

    assert result.comments[0].line == 2


def test_request_sets_limits_and_json_schema(api: FakeAPI) -> None:
    ask(api.llm(Settings(ANTHROPIC_API_KEY="k", max_tokens=123, effort="low")))

    body = api.sent_body()
    assert body["max_tokens"] == 123
    output_config = body["output_config"]
    assert isinstance(output_config, dict)
    assert output_config["effort"] == "low"
    assert output_config["format"]["type"] == "json_schema"
    assert output_config["format"]["schema"]["additionalProperties"] is False


def test_refusal_fallback_is_requested_for_supported_models(api: FakeAPI) -> None:
    ask(api.llm())

    assert api.sent_body()["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in api.requests[0].headers["anthropic-beta"]


def test_no_fallback_for_other_models(api: FakeAPI) -> None:
    ask(api.llm(Settings(ANTHROPIC_API_KEY="k", model="claude-haiku-5-5")))

    assert "fallbacks" not in api.sent_body()
    assert "anthropic-beta" not in api.requests[0].headers


def test_server_errors_are_retried_twice_then_fail(api: FakeAPI) -> None:
    api.responses = [overloaded()]

    with pytest.raises(LLMError, match="request failed"):
        ask(api.llm())

    assert len(api.requests) == 3


def test_retry_can_succeed(api: FakeAPI) -> None:
    api.responses = [overloaded(), reply()]

    assert ask(api.llm()).comments == []
    assert len(api.requests) == 2


def test_timeout_is_applied_and_reported(api: FakeAPI) -> None:
    api.responses = [httpx2.ReadTimeout("slow")]
    settings = Settings(ANTHROPIC_API_KEY="k", timeout_seconds=7, max_retries=0)

    with pytest.raises(LLMError, match="timed out"):
        ask(api.llm(settings))

    assert api.requests[0].extensions["timeout"]["read"] == 7


def test_client_error_is_not_retried(api: FakeAPI) -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "nope"}}
    api.responses = [httpx2.Response(400, json=error)]

    with pytest.raises(LLMError, match="nope"):
        ask(api.llm())

    assert len(api.requests) == 1


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [("refusal", "declined"), ("max_tokens", "cut off")],
)
def test_unfinished_replies_raise(api: FakeAPI, stop_reason: str, message: str) -> None:
    api.responses = [reply(stop_reason=stop_reason)]

    with pytest.raises(LLMError, match=message):
        ask(api.llm())


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        {"comments": [{**COMMENT, "line": 0}]},
        {"comments": [{**COMMENT, "severity": "urgent"}]},
        {"comments": [], "extra": 1},
    ],
)
def test_invalid_reply_raises(api: FakeAPI, payload: object) -> None:
    api.responses = [reply(payload)]

    with pytest.raises(LLMError, match="did not match"):
        ask(api.llm())


def test_oversized_prompt_is_not_sent(api: FakeAPI) -> None:
    llm = api.llm(Settings(ANTHROPIC_API_KEY="k", max_prompt_chars=10))

    with pytest.raises(LLMError, match="over the limit"):
        ask(llm, user="x" * 11)

    assert api.requests == []


@pytest.mark.parametrize("key", [None, ""])
def test_missing_api_key(key: str | None) -> None:
    settings = Settings() if key is None else Settings(ANTHROPIC_API_KEY=key)

    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        LLMClient(settings, on_request=print)


def test_successful_request_is_recorded_with_its_cost(api: FakeAPI) -> None:
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 300,
    }
    api.responses = [reply(usage=usage)]

    ask(api.llm())

    [record] = api.records
    assert (record.request_id, record.feature, record.status) == ("req_test", "review", "ok")
    assert (record.input_tokens, record.output_tokens) == (1000, 500)
    assert (record.cache_write_tokens, record.cache_read_tokens) == (200, 300)
    assert record.cost_usd == pytest.approx((1000 * 4 + 500 * 20 + 200 * 5 + 300 * 0.2) / 1e6)
    assert record.latency_seconds >= 0


def test_fallback_attempts_are_each_priced_at_their_own_model(api: FakeAPI) -> None:
    iterations = [
        {"type": "message", "model": "claude-opus-5-5", "input_tokens": 100, "output_tokens": 0},
        {
            "type": "fallback_message",
            "model": "claude-opus-4-8",
            "input_tokens": 100,
            "output_tokens": 10,
        },
    ]
    for entry in iterations:
        entry |= {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    usage = {"input_tokens": 100, "output_tokens": 10, "iterations": iterations}
    api.responses = [reply(model="claude-opus-4-8", usage=usage)]

    ask(api.llm())

    [record] = api.records
    assert (record.model, record.input_tokens) == ("claude-opus-4-8", 200)
    assert record.cost_usd == pytest.approx((100 * 4 + 100 * 5 + 10 * 25) / 1e6)


def test_unpriced_model_has_unknown_cost(api: FakeAPI) -> None:
    api.responses = [reply(model="claude-unreleased")]

    ask(api.llm())

    assert api.records[0].cost_usd is None


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (reply(stop_reason="refusal"), "refusal"),
        (reply(stop_reason="max_tokens"), "max_tokens"),
        (reply("not json"), "invalid_reply"),
    ],
)
def test_unusable_replies_are_still_recorded(
    api: FakeAPI, response: httpx2.Response, status: str
) -> None:
    api.responses = [response]

    with pytest.raises(LLMError):
        ask(api.llm())

    [record] = api.records
    assert record.status == status
    assert record.cost_usd is not None


def test_api_error_is_recorded_once_with_unknown_cost(api: FakeAPI) -> None:
    api.responses = [overloaded()]

    with pytest.raises(LLMError):
        ask(api.llm())

    [record] = api.records
    assert (record.status, record.input_tokens, record.cost_usd) == ("api_error", 0, None)


def test_nothing_is_logged_without_debug_level(
    api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="patchpilot"):
        ask(api.llm(), user="SOURCE CODE")

    assert "SOURCE CODE" not in caplog.text


def test_debug_log_has_prompt_but_no_api_key(
    api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    llm = api.llm(Settings(ANTHROPIC_API_KEY="secret-value"))

    with caplog.at_level(logging.DEBUG, logger="patchpilot"):
        ask(llm, user="SOURCE CODE")

    assert "SOURCE CODE" in caplog.text
    assert "secret-value" not in caplog.text
