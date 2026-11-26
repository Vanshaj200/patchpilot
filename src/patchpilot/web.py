"""A web page for reviewing code with a model and API key of your choice.

It has two modes. Local (the default) accepts connections from this computer only and may use
keys saved on it. Hosted is for a shared server: visitors must type their own key, nothing is
stored, and requests are rate limited.

In both modes the server never runs code (tests run inside the browser), never reads files named
in a pasted diff, and does not store a key typed into the page.
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
from patchpilot.ratelimit import RateLimiter
from patchpilot.review import review_diff
from patchpilot.store import RequestRecord, UsageStore

STATIC = Path(__file__).parent / "static"
SNIPPET = "snippet.py"  # the file name given to pasted Python code
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
MAX_BODY_BYTES = 1_000_000
# A hosted request has to finish while a visitor waits.
HOSTED_LIMITS: dict[str, object] = {"timeout_seconds": 60.0, "max_retries": 1}
HOSTED_PER_CLIENT_A_MINUTE = 10
HOSTED_TOTAL_A_MINUTE = 60
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


def create_app(
    http_client: httpx2.Client | None = None,
    hosted: bool = False,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    """Build the web app. Tests pass an `http_client` that fakes the model APIs."""
    app = FastAPI(title="PatchPilot", docs_url=None, redoc_url=None, openapi_url=None)
    if not hosted:
        # Refusing other host names stops a website from reaching this server through a
        # domain name that it points at 127.0.0.1 ("DNS rebinding"). A hosted server holds
        # no keys to protect, and is reached through its public name.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    elif limiter is None:
        limiter = RateLimiter(HOSTED_PER_CLIENT_A_MINUTE, HOSTED_TOTAL_A_MINUTE)

    @app.middleware("http")
    async def protect(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = _refusal(request, limiter) or await call_next(request)
        is_worker = request.url.path == WORKER_PATH
        response.headers["Content-Security-Policy"] = WORKER_POLICY if is_worker else CONTENT_POLICY
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if hosted:
            # Tells browsers to use HTTPS only, so a key is never sent unencrypted.
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
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

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def config() -> dict[str, bool]:
        return {"hosted": hosted}

    @app.get("/api/providers")
    def providers() -> list[dict[str, object]]:
        # A hosted server never looks for saved keys.
        settings = None if hosted else Settings()
        listed: list[dict[str, object]] = []
        for name, info in PROVIDERS.items():
            if hosted and info.key_field is None:
                continue  # a local model would be on the server, not the visitor's computer
            saved = None
            if settings is not None and info.key_field:
                saved = getattr(settings, info.key_field)
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

        return _run(job, action, http_client, hosted)

    @app.post("/api/gentests")
    def gentests(job: Job) -> dict[str, object]:
        # A diff shows only parts of files, which is not enough to write tests from.
        if job.kind != "python":
            raise HTTPException(400, "tests can only be generated from pasted Python code")

        def action(
            files: list[ChangedFile], llm: LLMClient, settings: Settings, workspace: Path
        ) -> dict[str, object]:
            return {"test_code": generate_tests(files, workspace, llm, settings)}

        return _run(job, action, http_client, hosted)

    return app


def _refusal(request: Request, limiter: RateLimiter | None) -> Response | None:
    """Check a request before it is read. Return the reply that rejects it, or None."""
    if request.method != "POST":
        return None
    # Any website can make your browser send a form or plain text here. Sending JSON needs a
    # permission this server never gives, so other sites cannot post on your behalf.
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        return JSONResponse({"detail": "send JSON"}, status_code=415)
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "the request is too large"}, status_code=413)
    if limiter is not None and not limiter.allow(_client_address(request)):
        return JSONResponse(
            {"detail": "too many requests; wait a minute and try again"},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    return None


def _client_address(request: Request) -> str:
    # Behind a host's proxy the visitor's address arrives in this header. A visitor can fake
    # it, which is why there is also a limit on all requests together.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _run(
    job: Job, action: Action, http_client: httpx2.Client | None, hosted: bool
) -> dict[str, object]:
    settings = _settings(job, hosted)
    records: list[RequestRecord] = []
    # A hosted server writes nothing about its visitors to disk.
    store = None if hosted else UsageStore(settings.db_path)

    def record(entry: RequestRecord) -> None:
        records.append(entry)
        if store is not None:
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


def _settings(job: Job, hosted: bool) -> Settings:
    info = PROVIDERS.get(job.provider)
    if info is None or (hosted and info.key_field is None):
        raise HTTPException(400, "unknown provider")
    key = job.api_key.get_secret_value().strip()
    if hosted and not key:
        raise HTTPException(400, "enter your own API key")
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
    if hosted:
        # The server's own keys and .env file must never pay for a visitor's request.
        overrides |= {
            other.key_field.upper(): "" for other in PROVIDERS.values() if other.key_field
        }
        # No request spacing: it exists for one account, and each visitor has their own.
        overrides |= HOSTED_LIMITS | {"_env_file": None, "requests_per_minute": 0}
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
