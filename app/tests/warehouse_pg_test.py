"""Discovery's warehouse statements (queries.py: the listing rows, the checks' and reads' claims, answers and
progress, the discovered tables) against a real Postgres with the Lakebase schema: each runs, takes the parameters
main.WarehouseReadStore passes, and changes only the rows it should.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import queries as q
import warehouse_reads as wr

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
LISTING = json.dumps({"via": "warehouse"})
# The page shown, for a check: it claims only the names the page lists.
ALL = ["eng_001", "eng_002", "eng_003"]


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    columns = [d.name for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _listed(conn, names):
    """A new listing of these databases, as the connection test through the warehouse saves it: its discovery id."""
    did = uuid.uuid4().hex[:12]
    conn.execute(q.insert_discovery_rows_sql(len(names)),
                 tuple(v for n in names for v in (did, "sqlserver", "sql_a", n, "listed", None, None, "op@example.com", LISTING)))
    return did


def _claim(conn, kind, did, first=(), limit=10, age_s=60):
    return [r["src_database"] for r in _rows(conn, q.warehouse_claim_sql(kind), (did, age_s, list(first), limit))]


def _row(conn, did, database):
    return _rows(conn, "select * from lakefed_ingest.discovery where discovery_id = %s and src_database = %s", (did, database))[0]


def test_a_listing_is_saved_once_with_how_it_was_listed(pg):
    did = _listed(pg, ["eng_002", "eng_001"])
    pg.execute(q.insert_discovery_rows_sql(1), (did, "sqlserver", "sql_a", "eng_001", "listed", None, None, "x", LISTING))
    rows = _rows(pg, "select src_database, status, listing, requested_by, finished_at from lakefed_ingest.discovery "
                     "where discovery_id = %s order by src_database", (did,))
    assert [(r["src_database"], r["status"]) for r in rows] == [("eng_001", "listed"), ("eng_002", "listed")]
    assert rows[0]["listing"] == {"via": "warehouse"} and rows[0]["requested_by"] == "op@example.com"
    assert all(r["finished_at"] is not None for r in rows)


def test_a_check_claims_only_the_names_asked_for_and_never_one_claimed_twice(pg):
    did = _listed(pg, [f"eng_00{i}" for i in range(1, 6)])
    page = ["eng_002", "eng_004", "eng_005"]
    # RETURNING keeps no order, so the claims are compared as sets.
    assert sorted(_claim(pg, wr.CHECK, did, first=page, limit=2)) == ["eng_002", "eng_004"]
    assert _claim(pg, wr.CHECK, did, first=page) == ["eng_005"]
    assert _claim(pg, wr.CHECK, did, first=page) == [] and _claim(pg, wr.CHECK, did) == []
    assert _rows(pg, q.warehouse_unchecked_sql(), (did, page)) == [{"unchecked": 3}]    # claimed, not answered yet
    # A claim with no statement after the age given is free again (the app stopped between claiming and starting).
    assert len(_claim(pg, wr.CHECK, did, first=[f"eng_00{i}" for i in range(1, 6)], age_s=0)) == 5


def test_a_read_claims_the_names_asked_for_first_then_the_rest(pg):
    did = uuid.uuid4().hex[:12]
    pg.execute(q.warehouse_named_rows_sql(3), (did, "sql_a", "eng_001", "op", "{}", did, "sql_a", "eng_002", "op", "{}",
                                               did, "sql_a", "eng_003", "op", "{}"))
    assert sorted(_claim(pg, wr.READ, did, first=["eng_003"], limit=2)) == ["eng_001", "eng_003"]
    assert _claim(pg, wr.READ, did) == ["eng_002"]


def test_a_started_statement_is_in_flight_until_cleared_then_claimed_again(pg):
    did = _listed(pg, ["eng_001", "eng_002"])
    _claim(pg, wr.CHECK, did, first=ALL)
    pg.execute(q.warehouse_started_sql(), ("s-1", did, "eng_001"))
    assert [(r["src_database"], r["statement_id"]) for r in _rows(pg, q.warehouse_in_flight_sql(wr.CHECK), (did,))] == \
        [("eng_001", "s-1")]
    assert _claim(pg, wr.CHECK, did, first=ALL, age_s=0) == ["eng_002"], "a database with a statement going is not claimed"
    pg.execute(q.warehouse_clear_sql(), (did, "eng_001"))
    assert _rows(pg, q.warehouse_in_flight_sql(wr.CHECK), (did,)) == []
    assert "claimed_at" not in _row(pg, did, "eng_001")["listing"]
    assert _claim(pg, wr.CHECK, did, first=ALL) == ["eng_001"]


def test_a_checks_answer_marks_the_database_open_or_closed_and_the_progress_counts_them(pg):
    did = _listed(pg, ["eng_001", "eng_002", "eng_003"])
    _claim(pg, wr.CHECK, did, first=ALL)
    pg.execute(q.warehouse_started_sql(), ("s-1", did, "eng_001"))
    pg.execute(q.warehouse_check_done_sql(), (True, None, 1000, did, "eng_001"))
    pg.execute(q.warehouse_check_done_sql(), (False, "no access", None, did, "eng_002"))
    one = _row(pg, did, "eng_001")
    assert one["listing"] == {"via": "warehouse", "open": True, "why": None} and one["tables"] == 1000
    assert _row(pg, did, "eng_002")["listing"]["why"] == "no access"
    assert _rows(pg, q.warehouse_progress_sql(wr.CHECK), (did,)) == \
        [{"databases": 3, "pending": 1, "open": 1, "closed": 1, "tables": 1000}]
    assert _rows(pg, q.warehouse_in_flight_sql(wr.CHECK), (did,)) == []
    assert _claim(pg, wr.CHECK, did, first=ALL, age_s=0) == ["eng_003"], "a checked database is not checked again"


def test_the_listing_page_and_its_totals_show_each_check_under_a_filter(pg):
    did = _listed(pg, ["eng_001", "eng_002", "fin_001"])
    pg.execute(q.warehouse_check_done_sql(), (True, None, 7, did, "eng_001"))
    pg.execute(q.warehouse_check_done_sql(), (False, "no access", None, did, "eng_002"))
    pg.execute(q.warehouse_check_done_sql(), (True, None, 3, did, "fin_001"))
    page = _rows(pg, q.discovery_listing_sql(), (did, "eng", 100, 0))
    assert [(r["src_database"], r["open"], r["why"], r["tables"]) for r in page] == \
        [("eng_001", True, None, 7), ("eng_002", False, "no access", None)]
    assert _rows(pg, q.discovery_totals_sql(), ("eng", did)) == [{
        "total": 3, "matched": 2, "open": 2, "closed": 1, "tables": 10, "matched_closed": 1, "matched_unknown": 0,
        "matched_tables": 7}]


def test_a_whole_server_read_takes_its_listing_under_the_filter_less_the_databases_the_login_cant_open(pg):
    listing = _listed(pg, ["eng_001", "eng_002", "eng_003", "fin_001"])
    pg.execute(q.warehouse_check_done_sql(), (True, None, 5, listing, "eng_001"))
    pg.execute(q.warehouse_check_done_sql(), (False, "no access", None, listing, "eng_002"))
    did = uuid.uuid4().hex[:12]
    pg.execute(q.warehouse_read_rows_sql(), (did, "op@example.com", "{}", listing, "eng"))
    rows = _rows(pg, "select src_database, status, src_connection, requested_by, listing, finished_at "
                     "from lakefed_ingest.discovery where discovery_id = %s order by src_database", (did,))
    assert [(r["src_database"], r["status"]) for r in rows] == [("eng_001", "running"), ("eng_003", "running")]
    assert rows[0]["src_connection"] == "sql_a" and rows[0]["listing"] == {"via": "warehouse", "whole_server": True}
    assert rows[0]["finished_at"] is None
    assert _rows(pg, q.warehouse_left_out_sql(), (listing, "eng")) == [{"left_out": 1}]
    assert _rows(pg, q.warehouse_left_out_sql(), (listing, "fin")) == [{"left_out": 0}]


def test_a_read_of_named_databases_saves_each_ones_tables_once_and_finishes_once(pg):
    did = uuid.uuid4().hex[:12]
    pg.execute(q.warehouse_named_rows_sql(2), (did, "sql_a", "eng_001", "op", "{}", did, "sql_a", "eng_002", "op", "{}"))
    pg.execute(q.warehouse_named_rows_sql(1), (did, "sql_a", "eng_001", "op", "{}"))
    assert sorted(_claim(pg, wr.READ, did)) == ["eng_001", "eng_002"]
    pg.execute(q.warehouse_started_sql(), ("s-1", did, "eng_001"))
    assert _rows(pg, q.warehouse_in_flight_sql(wr.READ), (did,)) == [{"src_database": "eng_001", "statement_id": "s-1"}]
    summary = {"sch": "dbo", "tbl": "orders", "row_count": "12", "ct_enabled": "1", "select_cols": "id, amount",
               "excluded_cols": None, "pk_cols": "id", "pk_unreadable": "0", "pk_first_type": "int",
               "rowversion_col": None, "identity_col": None, "identity_type": None}
    found = wr.table_rows(did, "eng_001", [summary, {**summary, "tbl": "lines"}])
    insert = q.insert_discovered_tables_sql(wr.TABLE_COLUMNS, len(found))
    pg.execute(insert, tuple(v for row in found for v in row))
    pg.execute(insert, tuple(v for row in found for v in row))
    pg.execute(q.warehouse_read_done_sql(), ("ok", len(found), None, did, "eng_001"))
    pg.execute(q.warehouse_read_done_sql(), ("failed", None, "late answer", did, "eng_001"))
    pg.execute(q.warehouse_read_done_sql(), ("failed", None, "Login failed", did, "eng_002"))
    tables = _rows(pg, "select src_table, load_type, primary_key, row_count from lakefed_ingest.discovered_table "
                       "where discovery_id = %s order by src_table", (did,))
    assert tables == [{"src_table": "lines", "load_type": "ct", "primary_key": "id", "row_count": 12},
                      {"src_table": "orders", "load_type": "ct", "primary_key": "id", "row_count": 12}]
    done = _row(pg, did, "eng_001")
    assert (done["status"], done["tables"], done["error"]) == ("ok", 2, None)
    assert "statement_id" not in done["listing"] and done["finished_at"] is not None
    assert _row(pg, did, "eng_002")["error"] == "Login failed"
    assert _rows(pg, q.warehouse_progress_sql(wr.READ), (did,)) == \
        [{"databases": 2, "pending": 0, "done": 1, "failed": 1, "tables": 2}]
    pg.execute("delete from lakefed_ingest.discovered_table where discovery_id = %s", (did,))      # the test's own tidy-up
    assert _rows(pg, "select count(*) as n from lakefed_ingest.discovered_table where discovery_id = %s", (did,)) == [{"n": 0}]
