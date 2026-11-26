# PatchPilot

A Python CLI and GitHub Action that will review a git diff with the Claude API, generate pytest
tests for the changed code, and track token cost per request.

**Status:** milestone 2 of 8 (diff parser). No commands exist yet.

## Setup

Requires Python 3.11 or newer.

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Checks

CI runs these three commands on every pull request and on every push to `main`.

```sh
ruff check .
ruff format --check .
pytest
```
