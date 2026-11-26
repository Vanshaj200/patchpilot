import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from patchpilot.store import RequestRecord, SummaryRow, UsageStore

RECORD = RequestRecord(
    request_id="req_1",
    feature="review",
    model="claude-opus-5-5",
    status="ok",
    input_tokens=100,
    output_tokens=20,
    cache_write_tokens=5,
    cache_read_tokens=7,
    cost_usd=0.5,
    latency_seconds=1.5,
    tokens_saved=40,
)


def test_new_store_is_empty_and_creates_folders(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "deep" / "usage.db")

    assert store.summary() == []


def test_summary_groups_by_feature_and_model(tmp_path: Path) -> None:
    store = UsageStore(tmp_path / "usage.db")
    store.add(RECORD)
    store.add(replace(RECORD, request_id="req_2", cost_usd=None, tokens_saved=None))
    store.add(replace(RECORD, feature="gentests", model="claude-haiku-5-5"))

    assert store.summary() == [
        SummaryRow("gentests", "claude-haiku-5-5", 1, 100, 20, 5, 7, 0.5, 0, 40),
        SummaryRow("review", "claude-opus-5-5", 2, 200, 40, 10, 14, 0.5, 1, 40),
    ]


def test_data_survives_reopening(tmp_path: Path) -> None:
    UsageStore(tmp_path / "usage.db").add(RECORD)

    assert UsageStore(tmp_path / "usage.db").summary()[0].requests == 1


def test_text_is_stored_as_data_not_sql(tmp_path: Path) -> None:
    hostile = "x'); DROP TABLE requests; --"
    UsageStore(tmp_path / "usage.db").add(replace(RECORD, feature=hostile))

    with closing(sqlite3.connect(tmp_path / "usage.db")) as connection:
        assert connection.execute("SELECT feature FROM requests").fetchall() == [(hostile,)]
