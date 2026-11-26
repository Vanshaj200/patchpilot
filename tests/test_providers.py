"""Tests of the OpenAI-compatible backend against a fake API. See test_llm.py for Claude."""

import json
import time

import httpx2
import pytest
from conftest import COMMENT, FakeAPI, reply

from patchpilot.config import Settings
from patchpilot.llm import LLMError
from patchpilot.providers import CompatibleBackend, create_backend
from patchpilot.review import Review

LARGE = "nvidia/nemotron-3-ultra-550b-a55b"
SMALL = "nvidia/nemotron-3.5-lightning-30b-a3b"


def nvidia(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "NVIDIA_API_KEY": "nv-key",
        "provider": "nvidia",
        "requests_per_minute": 0,
    }
    return Settings(**(values | overrides))


def completion(
    content: str | None = None,
    finish_reason: str = "stop",
    usage: dict[str, object] | None | str = "default",
) -> httpx2.Response:
    if usage == "default":
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    body = {
        "id": "chat_1",
        "object": "chat.completion",
        "created": 1,
        "model": LARGE,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"comments": [COMMENT]}) if content is None else content,
                    "reasoning_content": "thinking {not json}",
                },
            }
        ],
        "usage": usage,
    }
    return httpx2.Response(200, json=body, headers={"x-request-id": "nv_req"})


def ask(api: FakeAPI, settings: Settings | None = None) -> Review:
    llm = api.llm(settings or nvidia())
    return llm.complete(system="Review this.", user="diff", schema=Review, feature="review")


def test_default_models_depend_on_the_provider() -> None:
    assert (Settings().model, Settings().small_model) == (
        "claude-opus-5-5",
        "claude-haiku-5-5",
    )
    assert (nvidia().model, nvidia().small_model) == (LARGE, SMALL)
    assert nvidia(model="other/model").model == "other/model"


def test_provider_and_key_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHPILOT_PROVIDER", "nvidia")
    monkeypatch.setenv("NVIDIA_API_KEY", "secret-value")

    settings = Settings()

    assert settings.nvidia_api_key is not None
    assert settings.nvidia_api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(settings)


def test_request_shape(api: FakeAPI) -> None:
    api.responses = [completion()]

    ask(api, nvidia(max_tokens=123))

    request = api.requests[0]
    assert str(request.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer nv-key"
    body = api.sent_body()
    assert (body["model"], body["max_tokens"], body["stream"]) == (LARGE, 123, False)
    assert set(body) == {"model", "messages", "max_tokens", "stream"}
    system, user = body["messages"]
    assert system["content"].startswith("Review this.")
    assert '"additionalProperties": false' in system["content"]
    assert user["content"] == "diff"


def test_the_anthropic_key_is_never_sent_to_nvidia(api: FakeAPI) -> None:
    api.responses = [completion()]

    ask(api, nvidia(ANTHROPIC_API_KEY="claude-secret"))

    request = api.requests[0]
    assert "claude-secret" not in str(dict(request.headers)) + request.content.decode()


def test_reply_is_validated_and_recorded(api: FakeAPI) -> None:
    api.responses = [completion()]

    review = ask(api)

    assert review.comments[0].line == 2
    [record] = api.records
    assert (record.request_id, record.model, record.status) == ("nv_req", LARGE, "ok")
    assert (record.input_tokens, record.output_tokens, record.cache_read_tokens) == (100, 20, 0)
    # No price is configured for this model, so the cost is unknown rather than zero.
    assert record.cost_usd is None


def test_cached_tokens_and_configured_prices(api: FakeAPI) -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    api.responses = [completion(usage=usage)]
    price = {"input": 1, "output": 2, "cache_write": 0, "cache_read": 0.5}

    ask(api, nvidia(prices={LARGE: price}))

    [record] = api.records
    assert (record.input_tokens, record.cache_read_tokens) == (60, 40)
    assert record.cost_usd == pytest.approx((60 * 1 + 10 * 2 + 40 * 0.5) / 1e6)


def test_missing_usage_leaves_the_cost_unknown(api: FakeAPI) -> None:
    api.responses = [completion(usage=None)]
    price = {"input": 1, "output": 2, "cache_write": 0, "cache_read": 0}

    ask(api, nvidia(prices={LARGE: price}))

    assert api.records[0].cost_usd is None


@pytest.mark.parametrize(
    "wrapper",
    [
        "```json\n{}\n```",
        "<think>maybe {x}</think>\n{}",
        "Here is the review:\n{}\nDone.",
    ],
)
def test_json_is_found_inside_extra_text(api: FakeAPI, wrapper: str) -> None:
    payload = json.dumps({"comments": [COMMENT]})
    api.responses = [completion(wrapper.replace("{}", payload))]

    assert ask(api).comments[0].file == "calc.py"


@pytest.mark.parametrize("content", ["", "no json here", '{"comments": "wrong"}', "} {"])
def test_unusable_content_raises(api: FakeAPI, content: str) -> None:
    api.responses = [completion(content)]

    with pytest.raises(LLMError, match="did not match"):
        ask(api)

    assert api.records[0].status == "invalid_reply"


@pytest.mark.parametrize(
    ("finish_reason", "status"), [("length", "max_tokens"), ("content_filter", "refusal")]
)
def test_unfinished_replies(api: FakeAPI, finish_reason: str, status: str) -> None:
    api.responses = [completion(finish_reason=finish_reason)]

    with pytest.raises(LLMError):
        ask(api)

    assert api.records[0].status == status


def test_rate_limit_is_retried_twice_then_recorded(api: FakeAPI) -> None:
    busy = httpx2.Response(429, json={"detail": "slow down"}, headers={"retry-after-ms": "1"})
    api.responses = [busy]

    with pytest.raises(LLMError, match="nvidia API request failed"):
        ask(api)

    assert len(api.requests) == 3
    assert (api.records[0].status, api.records[0].cost_usd) == ("api_error", None)


def test_timeout_is_applied(api: FakeAPI) -> None:
    api.responses = [httpx2.ReadTimeout("slow")]

    with pytest.raises(LLMError, match="timed out"):
        ask(api, nvidia(timeout_seconds=7, max_retries=0))

    assert api.requests[0].extensions["timeout"]["read"] == 7


def test_reply_without_choices(api: FakeAPI) -> None:
    body = {"id": "x", "object": "chat.completion", "created": 1, "model": LARGE, "choices": []}
    api.responses = [httpx2.Response(200, json=body)]

    with pytest.raises(LLMError, match="no reply"):
        ask(api)


def test_missing_key() -> None:
    with pytest.raises(LLMError, match="NVIDIA_API_KEY is not set"):
        create_backend(Settings(provider="nvidia", ANTHROPIC_API_KEY="k"), None)


def test_cache_is_separate_for_each_provider(api: FakeAPI) -> None:
    api.responses = [completion()]
    ask(api, nvidia(model="same-name"))
    ask(api, nvidia(model="same-name"))
    assert len(api.requests) == 1

    api.responses = [reply({"comments": []})]
    ask(api, Settings(ANTHROPIC_API_KEY="k", model="same-name"))

    assert len(api.requests) == 2


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Time that only moves when someone sleeps. Returns the list of sleeps."""
    now, sleeps = [1000.0], []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(CompatibleBackend, "_next_start", {})
    return sleeps


def test_requests_are_spaced_across_clients(fake_clock: list[float]) -> None:
    settings = nvidia(requests_per_minute=30)

    CompatibleBackend(settings, None).wait_for_turn()
    CompatibleBackend(settings, None).wait_for_turn()

    assert fake_clock == [pytest.approx(2.0)]


def test_spacing_can_be_switched_off(fake_clock: list[float]) -> None:
    backend = CompatibleBackend(nvidia(), None)
    backend.wait_for_turn()
    backend.wait_for_turn()

    assert fake_clock == []


# provider, key variable, address, token limit field, JSON mode, large model, small model
EXPECTED = [
    (
        "nvidia",
        "NVIDIA_API_KEY",
        "https://integrate.api.nvidia.com/v1",
        "max_tokens",
        False,
        LARGE,
        SMALL,
    ),
    (
        "groq",
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1",
        "max_completion_tokens",
        True,
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
    ),
    (
        "deepseek",
        "DEEPSEEK_API_KEY",
        "https://api.deepseek.com",
        "max_tokens",
        True,
        "deepseek-v4-pro",
        "deepseek-flash",
    ),
    (
        "ollama",
        "OLLAMA_API_KEY",
        "https://ollama.com/v1",
        "max_tokens",
        False,
        "gpt-oss:120b",
        "gpt-oss:20b",
    ),
    (
        "ollama-local",
        None,
        "http://localhost:11434/v1",
        "max_tokens",
        True,
        "gemma4:e4b",
        "gemma4:e4b",
    ),
]
ALL_KEYS = {
    name: f"secret-for-{name}"
    for name in (
        "ANTHROPIC_API_KEY",
        "NVIDIA_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "OLLAMA_API_KEY",
    )
}


@pytest.mark.parametrize(
    ("provider", "variable", "address", "limit_field", "json_mode", "large", "small"), EXPECTED
)
def test_each_provider_gets_its_own_request(
    api: FakeAPI,
    provider: str,
    variable: str | None,
    address: str,
    limit_field: str,
    json_mode: bool,
    large: str,
    small: str,
) -> None:
    api.responses = [completion()]
    settings = Settings(provider=provider, requests_per_minute=0, **ALL_KEYS)

    ask(api, settings)

    assert (settings.model, settings.small_model) == (large, small)
    request = api.requests[0]
    assert str(request.url) == f"{address}/chat/completions"
    body = api.sent_body()
    assert body["model"] == large
    assert body[limit_field] == settings.max_tokens > 0
    assert set(body) - {"response_format"} == {"model", "messages", limit_field, "stream"}
    assert ("response_format" in body) is json_mode
    assert "JSON" in body["messages"][0]["content"]
    # Only this provider's key leaves the machine, and a local server gets none.
    sent = str(dict(request.headers)) + request.content.decode()
    for name, secret in ALL_KEYS.items():
        assert (secret in sent) is (name == variable)


@pytest.mark.parametrize(
    ("provider", "variable"), [(row[0], row[1]) for row in EXPECTED if row[1] is not None]
)
def test_each_provider_names_its_missing_key(provider: str, variable: str) -> None:
    others = {name: key for name, key in ALL_KEYS.items() if name != variable}

    with pytest.raises(LLMError, match=f"^{variable} is not set"):
        create_backend(Settings(provider=provider, **others), None)


def test_provider_defaults_for_limits() -> None:
    groq = Settings(provider="groq")

    assert (groq.max_tokens, groq.requests_per_minute) == (4096, 30)
    assert Settings(provider="deepseek").requests_per_minute == 0
    overridden = Settings(provider="groq", max_tokens=99, requests_per_minute=0)
    assert (overridden.max_tokens, overridden.requests_per_minute) == (99, 0)


def test_unreachable_local_server_gives_a_hint(api: FakeAPI) -> None:
    api.responses = [httpx2.ConnectError("refused")]

    with pytest.raises(LLMError, match="Is the Ollama server running"):
        ask(api, Settings(provider="ollama-local", max_retries=0))
