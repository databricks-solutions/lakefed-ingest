"""SQL statements through a SQL warehouse (the Statement Execution API), for discovery through a Unity Catalog
connection: the connection test, the server's database list and the tables of the chosen databases.

The app can't open a SQL Server connection itself: Unity Catalog keeps the connection's login and lends it only to
Databricks compute, a pro or serverless SQL warehouse or a cluster. The work is plain SQL (remote_query sends a query
in SQL Server's own language through the connection), so a small serverless warehouse, which starts in seconds, does
it without a cluster or a notebook. The app runs the statements as its own service principal, which has CAN_USE on
the warehouse and USE CONNECTION on the connections (the grants job).

The warehouse is the bundle's own (WAREHOUSE_ID, from the app resource), unless the bundle's app_warehouse_id names one
of the customer's own (APP_WAREHOUSE_ID), say one that runs in their network and reaches sources serverless can't. That
one must be Pro or serverless, on version 2025.35 or above, since remote_query doesn't run on a Classic warehouse; the
grants job gives the app CAN_USE on it. Manage shows which is in use (description).

A statement waits START_WAIT_S on the warehouse, then carries on there; the page asks again (read) until it ends, so
no request waits long and any app instance can answer."""
import os
from typing import List, Mapping, Optional, Tuple

import app_product
from lakebase import workspace

BUNDLES_OWN = "the bundle's own"
APP_WAREHOUSE_SETTING = "app_warehouse_id"


def chosen_warehouse(environ: Mapping[str, str]) -> Tuple[str, str]:
    """The warehouse the app's queries run on, and which setting chose it: APP_WAREHOUSE_ID (the bundle's
    app_warehouse_id) when it is set, else WAREHOUSE_ID (the bundle's own warehouse). The bundle sends it as
    "id:<app_warehouse_id>", since Databricks Apps refuses a blank env value, so "id:" alone, blank or missing all
    count as unset."""
    own = (environ.get("APP_WAREHOUSE_ID") or "").strip()
    if own.startswith("id:"):
        own = own[len("id:"):].strip()
    if own:
        return own, APP_WAREHOUSE_SETTING
    return (environ.get("WAREHOUSE_ID") or "").strip(), BUNDLES_OWN


WAREHOUSE_ID, CHOSEN_BY = chosen_warehouse(os.environ)
START_WAIT_S = 10       # the Statement Execution API takes 0, or 5 to 50 seconds
ENDED = ("SUCCEEDED", "FAILED", "CANCELED", "CLOSED")


class NotConfigured(RuntimeError):
    """The app has no SQL warehouse to run discovery on."""


def configured() -> bool:
    return bool(WAREHOUSE_ID)


def remote_query_statement(connection: str, database: str, query: str):
    """The statement that runs ``query`` on one database through a connection, and its parameters. The connection,
    the database and the query go in as parameter markers, so nothing needs escaping."""
    return ("SELECT * FROM remote_query(:conn, database => :db, query => :q)",
            [{"name": "conn", "value": connection}, {"name": "db", "value": database}, {"name": "q", "value": query}])


def start(statement: str, parameters: List[dict], wait_s: int = START_WAIT_S) -> dict:
    """Send a statement to the warehouse and wait up to wait_s for it: {statement_id, state, rows, error}, rows by
    column name once it has SUCCEEDED. With wait_s 0 it answers at once, and the statement carries on there."""
    from databricks.sdk.service.sql import (Disposition, ExecuteStatementRequestOnWaitTimeout, Format,
                                            StatementParameterListItem)
    if not configured():
        raise NotConfigured("no SQL warehouse is set for discovery (the app's WAREHOUSE_ID)")
    answer = workspace().statement_execution.execute_statement(
        statement=statement, warehouse_id=WAREHOUSE_ID, wait_timeout=f"{wait_s}s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE, disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
        parameters=[StatementParameterListItem(name=p["name"], value=p["value"]) for p in parameters],
        query_tags=app_product.query_tags("discovery"))
    return _view(answer)


def read(statement_id: str) -> dict:
    """A statement sent earlier, as start answers it."""
    return _view(workspace().statement_execution.get_statement(statement_id))


def cancel(statement_id: str) -> None:
    """Stop a statement still going (a discarded draft's read). One that has ended already is left as it is."""
    workspace().statement_execution.cancel_execution(statement_id)


def warehouse_state() -> str:
    """The warehouse's state (STARTING, RUNNING, STOPPED, ...), so the page can say it is starting; blank when it
    can't be read."""
    try:
        state = workspace().warehouses.get(WAREHOUSE_ID).state
        return getattr(state, "value", state) or ""
    except Exception:
        return ""


def description() -> dict:
    """The warehouse the app's queries run on, for Manage to show: its id, name, type (PRO, CLASSIC), whether it is
    serverless, its state, which setting chose it, and a plain problem line when remote_query can't run on it (a
    Classic warehouse) or it can't be read. Read only: the app starts and runs whatever this says."""
    out = {"id": WAREHOUSE_ID or None, "chosen_by": CHOSEN_BY, "name": None, "type": None, "serverless": None,
           "state": None, "problem": None}
    if not WAREHOUSE_ID:
        out["problem"] = "No SQL warehouse is set (WAREHOUSE_ID is empty), so Discover runs the discovery job instead."
        return out
    try:
        found = workspace().warehouses.get(WAREHOUSE_ID)
    except Exception as e:
        reason = str(e).strip().split("\n")[0][:300].rstrip(".")
        grant = (" The grants job (lakefed_ingest_app_grants) gives the app CAN_USE on it."
                 if CHOSEN_BY == APP_WAREHOUSE_SETTING else "")
        out["problem"] = f"The warehouse can't be read: {reason}.{grant}"
        return out
    kind = getattr(found.warehouse_type, "value", found.warehouse_type) or None
    state = getattr(found.state, "value", found.state) or None
    out.update(name=found.name, type=kind, serverless=bool(found.enable_serverless_compute), state=state)
    if kind == "CLASSIC":
        out["problem"] = ("This is a Classic warehouse, and remote_query doesn't run on one: set app_warehouse_id to a "
                          "Pro or serverless warehouse on version 2025.35 or above.")
    return out


def _view(answer) -> dict:
    status = answer.status
    state = getattr(status.state, "value", status.state) if status and status.state else "PENDING"
    error = (status.error.message or "").strip().split("\n")[0] if status and status.error else None
    rows: Optional[List[dict]] = _rows(answer) if state == "SUCCEEDED" else None
    return {"statement_id": answer.statement_id, "state": state, "rows": rows, "error": error}


def _rows(answer) -> List[dict]:
    """Every row of a finished statement, by column name, its later chunks read in turn."""
    columns = [c.name for c in answer.manifest.schema.columns]
    data = answer.result
    rows = list(data.data_array or []) if data else []
    following = data.next_chunk_index if data else None
    while following is not None:
        data = workspace().statement_execution.get_statement_result_chunk_n(answer.statement_id, following)
        rows += data.data_array or []
        following = data.next_chunk_index
    return [dict(zip(columns, row)) for row in rows]
