"""A local web page for reviewing code with a model and API key of your choice.

It accepts connections from this computer only. The server never runs code (tests run inside the
browser) and never reads files named in a pasted diff. A key typed into the page is used for that
request and is not stored.
"""

import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Literal

import httpx2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from patchpilot.config import PROVIDERS, Settings
from patchpilot.diff import ChangedFile, DiffParseError, new_file_diff, parse_diff
from patchpilot.gentests import generate_tests
from patchpilot.llm import LLMClient, LLMError
from patchpilot.review import review_diff
from patchpilot.store import RequestRecord, UsageStore

STATIC = Path(__file__).parent / "static"
SNIPPET = "snippet.py"  # the file name given to pasted Python code
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
# The page may load only its own files, and no other site may put it in a frame.
CONTENT_POLICY = "default-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
# The test worker runs untrusted code. It may load and contact the Pyodide download address and
# nothing else, which in particular excludes this server.
PYODIDE_SOURCE = "https://cdn.jsdelivr.net/pyodide/v314.0.8/full/"
WORKER_PATH = "/static/runner.js"
WORKER_POLICY = (
    f"default-src 'none'; script-src {PYODIDE_SOURCE} 'wasm-unsafe-eval'; "
    f"connect-src {PYODIDE_SOURCE}; worker-src 'none'"
)

Action = Callable[[list[ChangedFile], LLMClient, Settings, Path], dict[str, object]]


class Job(BaseModel):
    """What the page sends."""

    provider: str
    model: str = Field(default="", max_length=200)  # empty: choose by the size of the change
    api_key: SecretStr = SecretStr("")  # empty: use the key saved in the environment or .env
    kind: Literal["diff", "python"] = "diff"
    text: str = Field(min_length=1, max_length=200_000)


def create_app(http_client: httpx2.Client | None = None) -> FastAPI:
    """Build the web app. Tests pass an `http_client` that fakes the model APIs."""
    app = FastAPI(title="PatchPilot", docs_url=None, redoc_url=None, openapi_url=None)
    # Refusing other host names stops a website from reaching this server through a domain
    # name that it points at 127.0.0.1 ("DNS rebinding").
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.middleware("http")
    async def protect(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Any website can make your browser send a form or plain text here. Sending JSON
        # needs a permission this server never gives, so other sites cannot spend your key.
        content_type = request.headers.get("content-type", "").split(";")[0]
        if request.method == "POST" and content_type != "application/json":
            return JSONResponse({"detail": "send JSON"}, status_code=415)
        response = await call_next(request)
        is_worker = request.url.path == WORKER_PATH
        response.headers["Content-Security-Policy"] = WORKER_POLICY if is_worker else CONTENT_POLICY
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default reply repeats the input, which holds the key and the code.
        problems = [
            f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}"
            for item in error.errors()
        ]
        return JSONResponse({"detail": "; ".join(problems)}, status_code=422)

    @app.get("/")
    def page() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/providers")
    def providers() -> list[dict[str, object]]:
        settings = Settings()
        listed: list[dict[str, object]] = []
        for name, info in PROVIDERS.items():
            saved = getattr(settings, info.key_field) if info.key_field else None
            listed.append(
                {
                    "name": name,
                    "large_model": info.large_model,
                    "small_model": info.small_model,
                    "needs_key": info.key_field is not None,
                    # Only whether a key exists. Its value never goes to the browser.
                    "has_saved_key": saved is not None and bool(saved.get_secret_value()),
                }
            )
        return listed

    @app.post("/api/review")
    def review(job: Job) -> dict[str, object]:
        def action(
            files: list[ChangedFile], llm: LLMClient, settings: Settings, workspace: Path
        ) -> dict[str, object]:
            result = review_diff(files, llm, settings, workspace)
            return result.model_dump(mode="json")

        return _run(job, action, http_client)

    @app.post("/api/gentests")
    def gentests(job: Job) -> dict[str, object]:
        # A diff shows only parts of files, which is not enough to write tests from.
        if job.kind != "python":
            raise HTTPException(400, "tests can only be generated from pasted Python code")

        def action(
            files: list[ChangedFile], llm: LLMClient, settings: Settings, workspace: Path
        ) -> dict[str, object]:
            return {"test_code": generate_tests(files, workspace, llm, settings)}

        return _run(job, action, http_client)

    return app


def _run(job: Job, action: Action, http_client: httpx2.Client | None) -> dict[str, object]:
    settings = _settings(job)
    records: list[RequestRecord] = []
    store = UsageStore(settings.db_path)

    def record(entry: RequestRecord) -> None:
        records.append(entry)
        store.add(entry)

    try:
        llm = LLMClient(settings, on_request=record, http_client=http_client)
        # An empty folder: a pasted diff must never make the server read its own files.
        with tempfile.TemporaryDirectory(prefix="patchpilot-web-") as folder:
            workspace = Path(folder)
            text = job.text.replace("\r\n", "\n")
            if job.kind == "python":
                (workspace / SNIPPET).write_text(text, encoding="utf-8")
                text = new_file_diff(SNIPPET, text)
            files = parse_diff(text.rstrip("\n") + "\n")
            result = action(files, llm, settings, workspace)
    except DiffParseError as error:
        raise HTTPException(400, f"this is not a git diff: {error}") from error
    except LLMError as error:
        message = str(error)
        key = job.api_key.get_secret_value()
        if key:
            message = message.replace(key, "***")
        # "error" means we refused to send (such as no key); anything else came back from the API.
        raise HTTPException(400 if error.status == "error" else 502, message) from error
    return result | {"usage": _usage(records)}


def _settings(job: Job) -> Settings:
    info = PROVIDERS.get(job.provider)
    if info is None:
        raise HTTPException(400, "unknown provider")
    model = job.model.strip()
    overrides: dict[str, object] = {
        "provider": job.provider,
        # Empty names mean this provider's defaults, whatever .env says for another provider.
        "model": model,
        "small_model": "",
        # Other people's code is not written to the local reply cache.
        "response_cache": False,
    }
    if model:
        overrides["routing"] = False  # a chosen model is used for every change
    key = job.api_key.get_secret_value().strip()
    if key and info.key_field:
        overrides[info.key_field.upper()] = key
    return Settings(**overrides)


def _usage(records: Sequence[RequestRecord]) -> dict[str, object] | None:
    """Totals for the page. The cost is None if any part of it is unknown."""
    if not records:
        return None
    costs = [record.cost_usd for record in records if record.cost_usd is not None]
    return {
        "model": records[-1].model,
        "input_tokens": sum(
            r.input_tokens + r.cache_write_tokens + r.cache_read_tokens for r in records
        ),
        "output_tokens": sum(record.output_tokens for record in records),
        "cost_usd": sum(costs) if len(costs) == len(records) else None,
        "seconds": round(sum(record.latency_seconds for record in records), 1),
    }
