import pytest
from conftest import DIFF

from patchpilot.config import Settings
from patchpilot.diff import ChangedFile, parse_diff
from patchpilot.router import choose_model


def change(added: list[str], name: str = "app.py") -> ChangedFile:
    body = "".join(f"+{line}\n" for line in added)
    text = (
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n"
        f"@@ -0,0 +1,{len(added)} @@\n{body}"
    )
    return parse_diff(text)[0]


def test_small_change_goes_to_the_small_model() -> None:
    route = choose_model(parse_diff(DIFF), Settings())

    assert route.model == "claude-haiku-5-5"
    assert "small change" in route.reason


def test_routing_off_always_uses_the_large_model() -> None:
    route = choose_model(parse_diff(DIFF), Settings(routing=False))

    assert (route.model, route.reason) == ("claude-opus-5-5", "routing is off")


def test_many_files_use_the_large_model() -> None:
    route = choose_model([change(["a = 1"]), change(["b = 1"], "b.py")], Settings())

    assert route.model == "claude-opus-5-5"


@pytest.mark.parametrize(("lines", "model"), [(20, "claude-haiku-5-5"), (21, "claude-opus-5-5")])
def test_changed_line_limit(lines: int, model: str) -> None:
    assert choose_model([change(["x = 1"] * lines)], Settings()).model == model


@pytest.mark.parametrize(
    "line",
    [
        "if password == guess:",
        "cursor.execute(query)",
        "eval(text)",
        "API_TOKEN = x",
        "async def f():",
    ],
)
def test_risky_lines_use_the_large_model(line: str) -> None:
    route = choose_model([change([line])], Settings())

    assert (route.model, route.reason) == (
        "claude-opus-5-5",
        "a changed line matches the risky pattern",
    )


def test_removed_lines_are_checked_too() -> None:
    text = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +0,0 @@\n-check_password(user)\n"
    )

    assert choose_model(parse_diff(text), Settings()).model == "claude-opus-5-5"


def test_unchanged_context_does_not_count() -> None:
    text = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n password = load()\n-x = 1\n+x = 2\n"
    )

    assert choose_model(parse_diff(text), Settings()).model == "claude-haiku-5-5"


def test_rules_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHPILOT_ROUTE_MAX_CHANGED_LINES", "100")
    monkeypatch.setenv("PATCHPILOT_ROUTE_RISKY_PATTERN", "money")
    monkeypatch.setenv("PATCHPILOT_SMALL_MODEL", "tiny")

    assert choose_model([change(["password = 1"] * 50)], Settings()).model == "tiny"
    assert choose_model([change(["money = 1"])], Settings()).model == "claude-opus-5-5"
