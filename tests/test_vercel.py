"""The files Vercel reads must keep the hosted app safe."""

import json
import runpy
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent


def test_entry_point_serves_the_hosted_app() -> None:
    app = runpy.run_path(str(ROOT / "app.py"))["app"]
    client = TestClient(app, base_url="https://patchpilot.example")

    assert client.get("/api/config").json() == {"hosted": True}


def test_static_files_are_not_moved_to_the_cdn() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    # On a CDN the test worker would lose its content policy.
    assert project["tool"]["vercel"]["fastapi"]["static"]["cdn"] is False


def test_one_request_with_its_retry_fits_the_time_limit() -> None:
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))

    assert 2 * 60 < config["functions"]["app.py"]["maxDuration"] <= 300


def test_secrets_are_never_uploaded() -> None:
    ignored = (ROOT / ".vercelignore").read_text(encoding="utf-8").splitlines()

    assert {".env", ".env.*", ".patchpilot", "*.log"} <= set(ignored)
