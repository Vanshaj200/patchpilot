import pytest
from conftest import COMMENT, DIFF, FakeAPI, reply

from patchpilot.diff import parse_diff
from patchpilot.prompts import REVIEW_PROMPTS
from patchpilot.review import review_diff


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_sends_the_chosen_prompt_and_only_the_hunks(api: FakeAPI, version: str) -> None:
    review_diff(parse_diff(DIFF), api.llm(), version)

    body = api.sent_body()
    assert body["system"] == REVIEW_PROMPTS[version]
    assert "sum(items[1:])" in str(body["messages"])


def test_comments_on_unknown_files_are_dropped(
    api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    invented = {**COMMENT, "file": "other.py"}
    api.responses = [reply({"comments": [COMMENT, invented]})]

    review = review_diff(parse_diff(DIFF), api.llm(), "v2")

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
def test_no_request_when_there_is_nothing_to_review(api: FakeAPI, text: str) -> None:
    assert review_diff(parse_diff(text), api.llm(), "v2").comments == []
    assert api.requests == []
