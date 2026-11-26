import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx2
import pytest
from conftest import COMMENT, DIFF, FakeAPI, reply
from fastapi.testclient import TestClient
from test_providers import completion
from typer.testing import CliRunner

from patchpilot import cli
from patchpilot.config import PROVIDERS, Settings
from patchpilot.web import create_app

CODE = "def total(items):\n    return sum(items[1:])\n"
SNIPPET_COMMENT = {**COMMENT, "file": "snippet.py"}


@pytest.fixture
def client(api: FakeAPI) -> TestClient:
    return TestClient(create_app(api.http_client()), base_url="http://127.0.0.1")


def job(**changes: object) -> dict[str, object]:
    base: dict[str, object] = {
        "provider": "anthropic",
        "api_key": "typed-key",
        "kind": "python",
        "text": CODE,
    }
    return base | changes


def test_page_and_its_files_are_served(client: TestClient) -> None:
    page = client.get("/")

    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    for name in ("app.js", "style.css"):
        assert client.get(f"/static/{name}").status_code == 200


def test_page_has_no_inline_script_and_no_unsafe_html() -> None:
    static = Path(cli.__file__).parent / "static"

    assert "<script>" not in (static / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in (static / "app.js").read_text(encoding="utf-8")


def test_providers_show_whether_a_key_is_saved_but_not_the_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "saved-groq-key")

    response = client.get("/api/providers")

    listed = {item["name"]: item for item in response.json()}
    assert set(listed) == set(PROVIDERS)
    assert listed["groq"]["has_saved_key"]
    assert not listed["nvidia"]["has_saved_key"]
    assert not listed["ollama-local"]["needs_key"]
    assert "saved-groq-key" not in response.text


def test_review_of_pasted_code(client: TestClient, api: FakeAPI) -> None:
    api.responses = [reply({"comments": [SNIPPET_COMMENT]}, model="claude-haiku-5-5")]

    response = client.post("/api/review", json=job())

    assert response.status_code == 200
    body = response.json()
    assert body["comments"] == [SNIPPET_COMMENT]
    assert body["usage"]["input_tokens"] == 100
    assert body["usage"]["cost_usd"] == pytest.approx((100 * 0.1 + 20 * 0.5) / 1e6)
    assert api.requests[0].headers["authorization"] == "Bearer typed-key"


def test_review_of_a_diff(client: TestClient, api: FakeAPI) -> None:
    api.responses = [reply({"comments": [COMMENT]})]
    pasted = DIFF.replace("\n", "\r\n") + "\r\n\r\n"

    response = client.post("/api/review", json=job(kind="diff", text=pasted))

    assert response.json()["comments"] == [COMMENT]


def test_typed_key_and_model_go_to_the_chosen_provider(
    client: TestClient, api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "saved-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-provider-key")
    monkeypatch.setenv("PATCHPILOT_MODEL", "model-meant-for-another-provider")
    api.responses = [completion()]

    client.post("/api/review", json=job(provider="groq", model=" my/model "))

    request = api.requests[0]
    assert request.url.host == "api.groq.com"
    assert request.headers["authorization"] == "Bearer typed-key"
    assert api.sent_body()["model"] == "my/model"
    everything = str(dict(request.headers)) + request.content.decode()
    assert "saved-key" not in everything
    assert "other-provider-key" not in everything


def test_empty_key_uses_the_saved_one(
    client: TestClient, api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "saved-key")

    client.post("/api/review", json=job(api_key=""))

    assert api.requests[0].headers["authorization"] == "Bearer saved-key"


@pytest.mark.parametrize(
    ("model", "used"),
    [("", "claude-haiku-5-5"), ("claude-sonnet-5-5", "claude-sonnet-5-5")],
)
def test_empty_model_is_chosen_automatically(
    client: TestClient, api: FakeAPI, model: str, used: str
) -> None:
    client.post("/api/review", json=job(model=model))

    assert api.sent_body()["model"] == used


def test_generates_tests_without_running_them(client: TestClient, api: FakeAPI) -> None:
    code = "def test_total():\n    raise SystemExit('must not run')\n"
    api.responses = [reply({"test_code": code})]

    response = client.post("/api/gentests", json=job())

    assert response.json()["test_code"] == code
    assert "Module: snippet" in str(api.sent_body()["messages"])


def test_code_without_functions_gets_no_tests(client: TestClient, api: FakeAPI) -> None:
    response = client.post("/api/gentests", json=job(text="x = 1\n"))

    assert response.json() == {"test_code": None, "usage": None}
    assert api.requests == []


def test_tests_cannot_be_generated_from_a_diff(client: TestClient, api: FakeAPI) -> None:
    response = client.post("/api/gentests", json=job(kind="diff", text=DIFF))

    assert response.status_code == 400
    assert api.requests == []


def test_a_diff_cannot_make_the_server_read_its_files(client: TestClient, api: FakeAPI) -> None:
    Path("calc.py").write_text("SERVER_SECRET = 1\n" * 50)

    response = client.post("/api/review", json=job(kind="diff", text=DIFF))

    assert response.status_code == 200
    assert "SERVER_SECRET" not in api.requests[0].content.decode()
    with closing(sqlite3.connect(Settings().db_path)) as connection:
        assert connection.execute("SELECT tokens_saved FROM requests").fetchone() == (None,)


def test_only_counts_are_stored(client: TestClient, api: FakeAPI) -> None:
    client.post("/api/review", json=job())
    client.post("/api/review", json=job())

    assert len(api.requests) == 2  # no reply cache for web requests
    assert not Settings().cache_dir.exists()
    stored = Settings().db_path.read_bytes()
    assert b"typed-key" not in stored
    assert b"items[1:]" not in stored


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"api_key": ""}, "ANTHROPIC_API_KEY is not set"),
        ({"provider": "unknown"}, "unknown provider"),
        ({"kind": "diff", "text": "hello"}, "not a git diff"),
    ],
)
def test_problems_are_explained(
    client: TestClient, api: FakeAPI, changes: dict[str, object], message: str
) -> None:
    response = client.post("/api/review", json=job(**changes))

    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert api.requests == []


def test_api_errors_do_not_repeat_the_key(client: TestClient, api: FakeAPI) -> None:
    error = {"type": "error", "error": {"type": "authentication_error", "message": "bad typed-key"}}
    api.responses = [httpx2.Response(401, json=error)]

    response = client.post("/api/review", json=job())

    assert response.status_code == 502
    assert "typed-key" not in response.text
    assert "***" in response.json()["detail"]


@pytest.mark.parametrize("changes", [{"kind": "other"}, {"text": ""}, {"model": "m" * 201}])
def test_invalid_input_is_rejected_without_repeating_it(
    client: TestClient, api: FakeAPI, changes: dict[str, object]
) -> None:
    response = client.post("/api/review", json=job(**changes))

    assert response.status_code == 422
    assert "typed-key" not in response.text
    assert "items[1:]" not in response.text
    assert api.requests == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
def test_requests_another_website_could_send_are_refused(
    client: TestClient, api: FakeAPI, content_type: str
) -> None:
    response = client.post(
        "/api/review", content=json.dumps(job()), headers={"content-type": content_type}
    )

    assert response.status_code == 415
    assert api.requests == []


def test_no_cross_origin_permission_is_given(client: TestClient) -> None:
    response = client.options(
        "/api/review",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_other_host_names_are_refused(api: FakeAPI) -> None:
    outsider = TestClient(create_app(api.http_client()), base_url="http://evil.example")

    assert outsider.get("/api/providers").status_code == 400
    assert outsider.post("/api/review", json=job()).status_code == 400
    assert api.requests == []


def test_web_command_listens_on_this_computer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    started: dict[str, object] = {}

    def fake_run(app: object, **options: object) -> None:
        started.update(options)

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = CliRunner().invoke(cli.app, ["web", "--port", "9000"])

    assert result.exit_code == 0
    assert started == {"host": "127.0.0.1", "port": 9000}
    assert "http://127.0.0.1:9000" in result.stdout
