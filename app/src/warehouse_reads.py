"""Discovery's per-database statements on the SQL warehouse: whether the connection's login can open each database
(and how many tables it has), and the tables of the databases chosen.

Each database is one statement through the connection (remote_query). The statements are kept going a few at a time
(IN_FLIGHT, per kind), and each step of the work (advance) does what one request can: it reads the statements still
going, saves what ended, and starts more. The page calls it every second or two while it waits, and re-discovery calls
it in a loop, so no request waits long and no thread has to stay alive between them.

Where each database stands is kept on its discovery row in Lakebase (discovery.listing, a JSON object): the statement
going for it, when a request claimed it, and for a check whether the login can open it and why not. A request claims
the databases it starts (FOR UPDATE SKIP LOCKED), so two app instances or two open pages never start the same one
twice; a claim with no statement after CLAIM_S (the app stopped in between) is free again.

The store passed in holds the Lakebase calls (main.WarehouseReadStore), and the warehouse the Statement Execution
calls (warehouse.py), so this module is tested with fakes."""
import re
from typing import Iterable, List, Optional

import source_catalog

CLAIM_S = 60
# The two kinds of work: a check of a listed database (status 'listed'), and a read of a chosen one (status 'running').
CHECK, READ = "check", "read"
# Statements kept going at once for one discovery, by kind. The bundle's warehouse is 2X-Small with up to 2 clusters,
# and a serverless warehouse takes about 10 statements at once per cluster before it queues them. A read goes on in
# the background while the person fills in the form, so it keeps to 8 and leaves room for what they wait on: the
# connection test's listing (one statement, sent straight to the warehouse) and the check of the page shown.
IN_FLIGHT = {CHECK: 16, READ: 8}
# How a database is checked: opening it is the test, and the count says how many tables choosing it takes.
ACCESS_SQL = "SELECT COUNT(*) AS tables FROM sys.tables WHERE is_ms_shipped = 0"
TABLE_COLUMNS = ("discovery_id", "src_database", "src_schema", "src_table", "row_count", "size_mb", "ct_enabled",
                 "primary_key", "select_list", "excluded_columns", "rowversion_column", "identity_column", "load_type",
                 "cursor_column", "partition_candidate", "notes")

_NO_ACCESS = re.compile(r"Cannot open database|Login failed|not able to access the database|"
                        r"is not able to access|The server principal .* is not able to|permission was denied", re.I)
_OFFLINE = re.compile(r"is not currently available|offline|being recovered|in the middle of a restore", re.I)


def first_line(error: Optional[str]) -> str:
    return str(error or "").strip().split("\n")[0][:500]


def no_access(error: Optional[str]) -> bool:
    """Whether a database's error says the connection's login can't open it."""
    return bool(_NO_ACCESS.search(first_line(error)))


def why_closed(error: Optional[str]) -> str:
    """Why the login can't open a database, in plain words, then the server's own message."""
    message = first_line(error)
    if _NO_ACCESS.search(message):
        plain = "the connection's login has no access to this database: ask the server's admin for a user in it"
    elif _OFFLINE.search(message):
        plain = "the database isn't available on the server now (offline or restoring): check it again later"
    else:
        plain = "the database couldn't be opened through the connection"
    return f"{plain}. The server said: {message}" if message else plain


def statement_for(kind: str, connection: str, database: str):
    """The statement and parameters for one database: the check, or the read of its tables."""
    import warehouse
    query = ACCESS_SQL if kind == CHECK else source_catalog.TABLE_SUMMARY_SQL
    return warehouse.remote_query_statement(connection, database, query)


def table_rows(discovery_id: str, database: str, rows: Iterable[dict]) -> List[tuple]:
    """A read's rows as discovered_table rows (TABLE_COLUMNS), with the recommendations the discovery job makes."""
    found = source_catalog.assemble_summary(rows)
    return [tuple({**t, "discovery_id": discovery_id, "src_database": database}.get(c) for c in TABLE_COLUMNS)
            for t in found]


def count_of(rows: Optional[List[dict]]) -> Optional[int]:
    try:
        return int((rows or [{}])[0].get("tables"))
    except (TypeError, ValueError):
        return None


def advance(store, warehouse, kind: str, discovery_id: str, connection: str, first: Iterable[str] = ()) -> dict:
    """One step of a discovery's checks or reads: read the statements going, save the ended ones, start more (the
    names in ``first`` before the rest), and say where it stands (store.progress). A statement that can't be read any
    more (it expired, or the warehouse lost it) is cleared, so its database is started again."""
    going = store.in_flight(kind, discovery_id)
    for database, statement_id in going:
        try:
            answer = warehouse.read(statement_id)
        except Exception:
            store.clear(kind, discovery_id, database)
            continue
        if answer["state"] not in warehouse.ENDED:
            continue
        ok = answer["state"] == "SUCCEEDED"
        if kind == CHECK:
            store.finish_check(discovery_id, database, ok, count_of(answer["rows"]) if ok else None,
                               None if ok else why_closed(answer.get("error")))
        elif ok:
            store.finish_read(discovery_id, database, table_rows(discovery_id, database, answer["rows"] or []), None)
        else:
            store.finish_read(discovery_id, database, None, first_line(answer.get("error")) or
                              f"the read ended {answer['state']}")
    still = len(store.in_flight(kind, discovery_id))
    for database in store.claim(kind, discovery_id, list(first), max(0, IN_FLIGHT[kind] - still), CLAIM_S):
        statement, parameters = statement_for(kind, connection, database)
        try:
            answer = warehouse.start(statement, parameters, wait_s=0)    # sent together; the next step collects them
        except Exception as e:
            store.clear(kind, discovery_id, database)
            return {**store.progress(kind, discovery_id), "error": first_line(str(e)), "warehouse": ""}
        store.started(kind, discovery_id, database, answer["statement_id"])
    progress = store.progress(kind, discovery_id)
    return {**progress, "state": "SUCCEEDED" if not progress["pending"] else "RUNNING",
            "warehouse": warehouse.warehouse_state() if progress["pending"] else "RUNNING"}
