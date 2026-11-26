from pathlib import Path

import pytest
from conftest import COMMENT, DIFF, FakeAPI, reply

from patchpilot.diff import parse_diff
from patchpilot.prompts import REVIEW_PROMPTS
from patchpilot.review import estimate_tokens_saved, review_diff


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_sends_the_chosen_prompt_and_only_the_hunks(
    api: FakeAPI, tmp_path: Path, version: str
) -> None:
    review_diff(parse_diff(DIFF), api.llm(), version, tmp_path)

    body = api.sent_body()
    assert body["system"] == REVIEW_PROMPTS[version]
    assert "sum(items[1:])" in str(body["messages"])


def test_comments_on_unknown_files_are_dropped(
    api: FakeAPI, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    invented = {**COMMENT, "file": "other.py"}
    api.responses = [reply({"comments": [COMMENT, invented]})]

    review = review_diff(parse_diff(DIFF), api.llm(), "v2", tmp_path)

    assert [comment.file for comment in review.comments] == ["calc.py"]
    assert "dropped 1 comments" in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "diff --git a/a.bin b/a.bin\nBinary files a/a.bin and b/a.bin differ\n",
        "diff --git a/a b/b\nrename from a\nrename to b\n",
    ],
)
def test_no_request_when_there_is_nothing_to_review(
    api: FakeAPI, tmp_path: Path, text: str
) -> None:
    assert review_diff(parse_diff(text), api.llm(), "v2", tmp_path).comments == []
    assert api.requests == []


def test_tokens_saved_compares_hunks_with_the_whole_file(api: FakeAPI, tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def total(items):\n" + "    # padding\n" * 200)

    review_diff(parse_diff(DIFF), api.llm(), "v2", tmp_path)

    saved = api.records[0].tokens_saved
    assert saved is not None
    assert saved > 500


def test_tokens_saved_is_unknown_without_the_files(tmp_path: Path) -> None:
    assert estimate_tokens_saved(parse_diff(DIFF), tmp_path) is None


def test_paths_outside_the_repository_are_not_read(tmp_path: Path) -> None:
    (tmp_path / "secret.py").write_text("password = 'x'\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    hostile = DIFF.replace("calc.py", "../secret.py")

    assert estimate_tokens_saved(parse_diff(hostile), repo) is None
