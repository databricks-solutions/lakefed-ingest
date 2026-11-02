"""The set-based SQL of a job's settings (job_settings_sql.py) against a real Postgres: it must read what
job_settings.read reads from the same rows, and an edit must leave the rows as job_settings.plan would.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

import job_settings as js
import job_settings_sql as jss
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
COLUMNS = ("task_collection", "src_type", "src_connection", "use_remote_query", "src_host", "src_port", "secret_key",
           "src_database", "src_schema", "src_table", "select_list", "primary_key", "load_type", "cursor_column",
           "write_mode", "sink_catalog", "sink_schema", "sink_table", "client_id", "client_attrs",
           "freshness_sla_minutes")
GROUPS = ["g1", "g2"]


def _row(group, database, table, load_type="ct", **over):
    base = dict(task_collection=group, src_type="sqlserver", src_connection="Conn_A", use_remote_query=True,
                src_host=None, src_port=None, secret_key=None, src_database=database, src_schema="dbo",
                src_table=table, select_list="id,qty", primary_key="id", load_type=load_type,
                cursor_column="rv" if load_type == "cursor" else None,
                write_mode="merge" if load_type == "cursor" else "append", sink_catalog="main", sink_schema="bronze",
                sink_table=table.lower(), client_id=database,
                client_attrs=json.dumps({"source_server": "conn_a", "source_database": database}),
                freshness_sla_minutes=60)
    base.update(over)
    return tuple(base[c] for c in COLUMNS)


FLEET = [_row("g1", "db_1", "Placeholder", "cdc", write_mode="merge"), _row("g1", "db_1", "OrderLine"), _row("g1", "db_1", "Invoice"), _row("g2", "db_2", "OrderLine"),
         _row("g2", "db_2", "Invoice"), _row("g2", "db_2", "Child_View", "full"),
         _row("g1", "db_1", "Stamp", "cursor"), _row("other", "X", "OrderLine")]


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_jss_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True, row_factory=dict_row) as c:
            for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
                c.execute(statement)
            yield c
    finally:
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _load(conn, rows):
    conn.cursor().executemany(f"insert into lakefed_ingest.control ({', '.join(COLUMNS)}) values "
                              f"({', '.join(['%s'] * len(COLUMNS))})", rows)


def _fetch(conn):
    return lambda sql, params=(): conn.execute(sql, params).fetchall()


def _rows(conn):
    return conn.execute(q.groups_control_rows_sql(), (GROUPS,)).fetchall()


CASES = {
    "fleet": FLEET,
    "mixed freshness and own targets": [_row("g1", "A", "T1", freshness_sla_minutes=60),
                                        _row("g1", "B", "T1", freshness_sla_minutes=240, client_id=None,
                                             sink_schema="bronze_b")],
    "jdbc with default port": [_row("g1", "Sales", "T1", src_connection=None, use_remote_query=False,
                                    src_host="SQL-1.corp", secret_key="Sales", client_id=None, client_attrs=None,
                                    sink_schema="bronze_sales")],
    "a template ranked far down": [_row("g1", "x", "x", src_connection="x", src_schema="x", client_id="xx",
                                        sink_table="x", client_attrs=None),
                                   _row("g1", "y", "x", src_connection="x", src_schema="x", client_id="yy",
                                        sink_table="x", client_attrs=None)],
    "identity columns in another order": [_row("g1", "A", "T1", client_attrs=json.dumps({"a": "A", "b": "x"})),
                                          _row("g1", "B", "T1", client_attrs=json.dumps({"b": "x", "a": "B"}))],
    "identity columns differ": [_row("g1", "A", "T1"),
                                _row("g1", "B", "T1", client_attrs=json.dumps({"source_server": "conn_a"}))],
}


@pytest.mark.parametrize("unity_catalog", [False, True])
@pytest.mark.parametrize("case", CASES)
def test_the_sql_read_agrees_with_the_row_by_row_read(conn, case, unity_catalog):
    _load(conn, CASES[case])
    expected = js.read(_rows(conn), unity_catalog)
    found = jss.read(_fetch(conn), GROUPS, unity_catalog)
    for name in expected:
        if "mixed" in expected[name]:
            assert "mixed" in found[name], name
        else:
            assert found[name] == expected[name], name


EDITS = {
    "identity and freshness": {"identity": {"source_key": "{server}/{database}"}, "freshness_sla_minutes": 90},
    "new targets": {"sink_schema": "silver", "sink_table": "{schema}_{table}"},
    "write mode with new targets": {"write_mode_changes": "merge", "write_mode_full": "merge", "sink_schema": "current"},
    "fan-in off with database targets": {"fan_in": False, "sink_schema": "bronze_{database}"},
    "client_id template": {"client_id": "{server}:{database}"},
    "a blank identity value": {"identity": {"region": "", "source_database": "{database}"}},
    "write mode in capitals with new targets": {"write_mode_full": " Merge ", "sink_schema": "current"},
}


@pytest.mark.parametrize("unity_catalog", [False, True])
@pytest.mark.parametrize("edit", EDITS)
def test_an_sql_edit_leaves_the_rows_as_the_row_by_row_plan_would(conn, edit, unity_catalog):
    _load(conn, FLEET)
    before = _rows(conn)
    planned = js.plan(before, EDITS[edit], unity_catalog)
    expected = [{**r, **planned.get(r["id"], {})} for r in before]
    now = jss.read(_fetch(conn), GROUPS, unity_catalog)
    check = jss.preview(_fetch(conn), GROUPS, EDITS[edit], now)

    assert check["errors"] == [] and check["summary"]["changed"] == len(planned)
    with conn.transaction():
        result = jss.apply(conn.cursor(), GROUPS, EDITS[edit], now, "op@example.com", "b1")
    assert result["changed"] == len(planned)
    after = _rows(conn)
    for want, got in zip(expected, after):
        for field in q.CONTROL_EDIT_COLUMNS:
            if field == "client_attrs":
                assert json.loads(got[field] or "null") == json.loads(want[field] or "null"), (field, got["id"])
            else:
                assert got[field] == want[field], (field, got["id"])
    logged = conn.execute("select count(*) as n from lakefed_ingest.control_change").fetchone()["n"]
    assert logged == len({(r["task_collection"], r["src_database"]) for r in before if r["id"] in planned})


def test_a_new_target_reseeds_the_change_tracked_and_cursor_tables_only(conn):
    _load(conn, FLEET)
    for r in _rows(conn):
        conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, strategy, "
                     "phase, status) values (%s, %s, %s, %s, %s, 'incremental', 'ok')",
                     (r["id"], r["src_database"], r["src_schema"], r["src_table"], r["load_type"]))
    now = jss.read(_fetch(conn), GROUPS)
    with conn.transaction():
        result = jss.apply(conn.cursor(), GROUPS, {"sink_schema": "silver"}, now, "op", "b2")
    assert (result["changed"], result["reseed"], result["flipped"], result["queued"]) == (7, 5, 5, 5)
    seeding = conn.execute("select count(*) as n from lakefed_ingest.checkpoint where phase = 'seeding'").fetchone()["n"]
    assert seeding == 5


def test_the_preview_refuses_what_the_rows_could_not_take(conn):
    _load(conn, FLEET)
    now = jss.read(_fetch(conn), GROUPS)
    mode = jss.preview(_fetch(conn), GROUPS, {"write_mode_changes": "merge"}, now)
    assert mode["errors"] == ["4 tables would change write mode on the same target: a change log and the current "
                              "rows can't share a table, so change the target names too"]
    clash = jss.preview(_fetch(conn), GROUPS, {"fan_in": False}, now)
    assert clash["errors"][0].startswith("2 targets would be written by more than one table")
    bad = jss.preview(_fetch(conn), GROUPS, {"client_id": "{database} x"}, now)
    assert bad["errors"][0].startswith("7 tables would get a client_id that is not allowed (e.g. db_1 x)")
    sla = jss.preview(_fetch(conn), GROUPS, {"freshness_sla_minutes": 0}, now)
    assert sla["errors"] == ["freshness target: a whole number of minutes, 1 or more, or none"]
    mode = jss.preview(_fetch(conn), GROUPS, {"write_mode_full": "upsert", "sink_schema": "x"}, now)
    assert mode["errors"] == ["write mode for full-reload tables: merge or append"]


def test_an_identity_column_named_like_a_source_column_or_twice_is_refused(conn):
    _load(conn, FLEET)
    now = jss.read(_fetch(conn), GROUPS)
    clash = jss.preview(_fetch(conn), GROUPS, {"identity": {"qty": "{database}"}}, now)
    assert clash["errors"][0].startswith("7 tables already have a column named like an identity column (e.g. qty)")
    twice = jss.preview(_fetch(conn), GROUPS, {"identity": {"Region": "{server}", "region": "{database}"}}, now)
    assert twice["errors"] == ["identity column region is given twice (names ignore case)"]
