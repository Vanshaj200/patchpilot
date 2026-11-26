"""SQLite log of API requests. It holds counts and costs, never prompts, code or keys."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    request_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cost_usd REAL,
    latency_seconds REAL NOT NULL,
    tokens_saved INTEGER
)
"""

_INSERT = """
INSERT INTO requests (
    request_id, feature, model, status, input_tokens, output_tokens,
    cache_write_tokens, cache_read_tokens, cost_usd, latency_seconds, tokens_saved
) VALUES (
    :request_id, :feature, :model, :status, :input_tokens, :output_tokens,
    :cache_write_tokens, :cache_read_tokens, :cost_usd, :latency_seconds, :tokens_saved
)
"""

# SUM skips NULL, so unknown costs are counted separately instead of passing as zero.
_SUMMARY = """
SELECT feature, model, COUNT(*), SUM(status = 'cache_hit'),
       SUM(input_tokens), SUM(output_tokens),
       SUM(cache_write_tokens), SUM(cache_read_tokens), TOTAL(cost_usd),
       SUM(cost_usd IS NULL), TOTAL(tokens_saved)
FROM requests
GROUP BY feature, model
ORDER BY feature, model
"""


@dataclass(frozen=True)
class RequestRecord:
    """One API request. cost_usd is None when it is unknown."""

    request_id: str
    feature: str
    model: str
    status: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float | None
    latency_seconds: float
    tokens_saved: int | None


@dataclass(frozen=True)
class SummaryRow:
    """Totals for one feature and model."""

    feature: str
    model: str
    requests: int
    cache_hits: int  # requests answered by the local response cache
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float
    unknown_cost_requests: int
    tokens_saved: int


class UsageStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def add(self, record: RequestRecord) -> None:
        # Named parameters keep values out of the SQL text, which prevents SQL injection.
        with self._connect() as connection:
            connection.execute(_INSERT, asdict(record))

    def summary(self) -> list[SummaryRow]:
        with self._connect() as connection:
            rows = connection.execute(_SUMMARY).fetchall()
        return [SummaryRow(*row[:-1], tokens_saved=int(row[-1])) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # "with connection" commits or rolls back; closing() is what actually closes it.
        with closing(sqlite3.connect(self._path)) as connection, connection:
            yield connection
