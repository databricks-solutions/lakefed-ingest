"""A recording stand-in for the Lakebase (psycopg) connection. The scale steps run the app's and the discover job's
SQL against it and read back what was sent: how many statements of each kind, and how many rows they carried. Nothing
connects anywhere; the statements are only counted, and ``answers`` can give rows back for the queries that read.

A statement counts as one statement. ``executemany`` counts one statement per parameter set, as psycopg sends it: a
row per round trip, even when the rows go out pipelined. A multi-row ``values (...), (...)`` statement counts as one
statement carrying as many rows as it has value groups.
"""
from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional, Sequence

TABLE_AFTER_VERB = re.compile(r"\b(?:into|update|from|join)\s+([a-z_][a-z_0-9.]*)", re.IGNORECASE)
VALUE_GROUP = re.compile(r"\(\s*%s")


def statement_kind(sql: str) -> str:
    """The verb and the table a statement touches, as in ``insert lakefed_ingest.discovered_table``."""
    verb = sql.strip().split(None, 1)[0].lower() if sql.strip() else "empty"
    match = TABLE_AFTER_VERB.search(sql)
    return f"{verb} {match.group(1).lower()}" if match else verb


def rows_in(sql: str) -> int:
    """Rows a single statement carries: its value groups, or one for a statement without a values list."""
    return max(1, len(VALUE_GROUP.findall(sql)))


class RecordingConnection:
    """What a Lakebase connection was asked to do. ``answers(sql)`` gives back the rows a read returns, if any."""

    def __init__(self, answers: Optional[Callable[[str, tuple], List[tuple]]] = None):
        self.statements: Counter = Counter()
        self.rows: Counter = Counter()
        self.transactions = 0
        self._answers = answers or (lambda sql, params: [])

    def record(self, sql: str, rows: int, count: int = 1) -> None:
        kind = statement_kind(sql)
        self.statements[kind] += count
        self.rows[kind] += rows

    def total_statements(self) -> int:
        return sum(self.statements.values())

    def total_rows(self) -> int:
        return sum(self.rows.values())

    def reads(self) -> int:
        """Statements that read (a select, or a lock taken by a select), as the Counter's kinds name them."""
        return sum(count for kind, count in self.statements.items() if kind.startswith("select"))

    def round_trips(self) -> int:
        """Round trips a real connection makes: each statement, and the BEGIN and COMMIT of each transaction."""
        return self.total_statements() + 2 * self.transactions

    @contextmanager
    def cursor(self) -> Iterator["RecordingCursor"]:
        yield RecordingCursor(self)

    @contextmanager
    def transaction(self) -> Iterator["RecordingCursor"]:
        self.transactions += 1
        yield RecordingCursor(self)

    def close(self) -> None:
        return None


class RecordingCursor:
    """The part of a psycopg cursor the app and the discover job use."""

    def __init__(self, connection: RecordingConnection):
        self._connection = connection
        self._result: List[tuple] = []

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params: Optional[Sequence] = None) -> None:
        self._connection.record(sql, rows_in(sql))
        self._result = self._connection._answers(sql, tuple(params or ()))

    def executemany(self, sql: str, parameter_sets: Sequence[Sequence]) -> None:
        count = len(parameter_sets)
        self._connection.record(sql, count, count=count)
        self._result = []

    def fetchall(self) -> List[tuple]:
        result, self._result = self._result, []
        return result

    def fetchone(self) -> Optional[tuple]:
        return self._result[0] if self._result else None


@contextmanager
def no_connections() -> Iterator[None]:
    """While the block runs, opening a Lakebase connection or a workspace client raises: a step that tries one fails
    loudly instead of reaching a database."""
    import psycopg

    import lakebase

    def refuse(*args, **kwargs):
        raise RuntimeError("the scale harness does not connect to a database or a workspace")

    saved = (lakebase.connect, lakebase.workspace, psycopg.connect)
    lakebase.connect = lakebase.workspace = psycopg.connect = refuse
    try:
        yield
    finally:
        lakebase.connect, lakebase.workspace, psycopg.connect = saved
