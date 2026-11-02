"""Monitoring views: the overview's progress and state counts, a collection's and a run's databases,
rows written per run, grouped failures and UC connections. SQL shape and Python logic here; the SQL
itself runs against a real Postgres in monitoring_pg_test.py."""
import json

import pytest
import queries as q

MONITORING_SQL = (q.servers_sql(), q.collection_databases_sql(), q.collection_database_rows_sql(),
                  q.run_databases_sql(), q.run_batch_rows_sql(), q.collection_batch_runs_sql(),
                  q.connection_databases_sql(), q.in_flight_sql(), q.in_flight_count_sql(), q.run_events_sql(),
                  q.group_runs_sql(), q.job_run_databases_sql(), q.run_failures_sql())


def test_monitoring_statements_are_parameterized_and_schema_qualified():
    for sql in MONITORING_SQL:
        assert "lakefed_ingest." in sql
        assert "{" not in sql            # every f-string placeholder was filled
    assert q.collection_databases_sql().count("%s") == 2
    assert q.collection_database_rows_sql().count("%s") == 1
    assert q.collection_batch_runs_sql().count("%s") == 2
    assert q.failed_tables_sql().count("%s") == 2
    assert q.connection_databases_sql().count("%s") == 2
    assert (q.in_flight_sql().count("%s"), q.in_flight_count_sql().count("%s")) == (2, 1)
    assert (q.group_runs_sql().count("%s"), q.job_run_databases_sql().count("%s"), q.run_failures_sql().count("%s")) == (3, 1, 1)
    assert q.run_events_sql().endswith(f"limit {q.RUN_EVENTS_LIMIT}")


def test_running_runs_report_progress_over_the_runs_tables():
    sql = q.running_runs_sql()
    for column in ("enabled_tables", "tables_checked", "last_activity_at", "server"):
        assert f"as {column}" in sql
    assert f"{q.LAST_SUCCESS} >= r.started_at" in sql and "coalesce(r.total, " in sql


def test_overview_adds_exclusive_states_and_rows_without_changing_existing_columns():
    sql = q.collections_overview_sql()
    for column in ("enabled_tables", "failing_tables", "seeding_tables", "sla_breached_tables", "last_run_status",
                   "state_current", "state_late", "state_first_load", "state_failing", "rows_24h", "databases"):
        assert column in sql
    assert "%s" not in sql


@pytest.mark.parametrize("detail,expected", [
    (json.dumps({"batch_rows": 5, "batch_rows_unknown": 2}), (5, 2)),
    ({"batch_rows": 7}, (7, 0)),
    ("not json", (0, 0)),
    ("[1, 2]", (0, 0)),
    (None, (0, 0)),
])
def test_batch_rows_reads_a_run_detail_safely(detail, expected):
    assert q.batch_rows(detail) == expected


@pytest.mark.parametrize("error,expected", [
    ("[FAILED_JDBC.CONNECTION] Failed to connect to the database: connection reset", "connection"),
    ("Read timed out after 30000 ms", "timeout"),
    ("The SELECT permission was denied on the object 'orders'", "permission"),
    ("Invalid object name 'dbo.orders'.", "missing"),
    ("Something else entirely", "other"),
    (None, "other"),
    # Near misses: a name that contains a cause word is not that cause.
    ("Invalid column name 'network_id'.", "missing"),
    ("Invalid column name 'timeout_ms'.", "missing"),
    ("Invalid column name 'socket_count'.", "missing"),
    ("Invalid column name 'network'.", "missing"),
    ("Invalid column name 'socket'.", "missing"),
    ("Invalid column name 'timeout'.", "missing"),
    ("Invalid object name 'dbo.network'.", "missing"),
    ("Invalid column name 'network status'.", "missing"),
    ('Invalid column name "network".', "missing"),
    ("Invalid object name [dbo].[network].", "missing"),
    ("connection reset while reading [dbo].[orders]", "connection"),
    ("Login timeout expired for 'reader'", "connection"),
    ('The TCP/IP connection to the host sql01, port 1433 has failed. Error: "Connection timed out: no further '
     'information.". Verify the connection properties.', "connection"),
    ("[FAILED_JDBC.CONNECTION] Failed to open the connection", "connection"),
    ("A network-related or instance-specific error occurred while establishing a connection", "connection"),
    ("java.net.SocketException: Connection reset", "connection"),
    ("The TCP/IP connection to the host failed: network is unreachable", "connection"),
    # The engine's own messages when Change Tracking is off on a table or a database (ct_engine.py).
    ("Change Tracking is not enabled on this source table (CHANGE_TRACKING_MIN_VALID_VERSION() is NULL); enable "
     "it, then the table re-seeds", "change_tracking"),
    ("Change Tracking is not enabled on this source database (CHANGE_TRACKING_CURRENT_VERSION() is NULL)",
     "change_tracking"),
])
def test_classify_error(error, expected):
    assert q.classify_error(error)[0] == expected


def test_error_signature_blanks_names_and_numbers_so_tables_group_together():
    a = q.error_signature("Invalid object name 'dbo.t0041'. Line 12")
    b = q.error_signature("Invalid object name 'dbo.t0838'. Line 7")
    assert a == b and "t0041" not in a


def test_group_errors_counts_tables_and_databases_per_cause():
    rows = [dict(finished_at=f"2026-10-23T07:{m:02d}:00+00:00", task_collection=c, src_database=d, src_schema="dbo",
                 src_table=t, error="[FAILED_JDBC.CONNECTION] Failed to connect to the database: connection reset")
            for m, c, d, t in ((51, "c7", "db_083", "t0041"), (46, "c7", "db_083", "t0838"),
                               (40, "c6", "db_073", "t0328"))]
    rows.append(dict(finished_at="2026-10-23T08:00:00+00:00", task_collection="c1", src_database="db_001",
                     src_schema="dbo", src_table="x", error="Invalid object name 'dbo.x'."))
    groups = q.group_errors(rows)
    assert [g["error_class"] for g in groups] == ["connection", "missing"]
    first = groups[0]
    assert first["tables"] == 3 and len(first["databases"]) == 2
    assert first["first_at"] < first["last_at"]
    assert first["databases"][1]["tables"] == ["dbo.t0041", "dbo.t0838"]


def test_group_errors_counts_a_table_failing_on_every_run_once():
    rows = [dict(finished_at=f"2026-10-23T{h:02d}:00:00+00:00", task_collection="c1", control_id=7, src_database="db1",
                 src_schema="dbo", src_table="orders", error="Read timed out after 30000 ms") for h in (9, 8, 7)]
    g = q.group_errors(rows)[0]
    assert (g["tables"], g["failures"]) == (1, 3)
    assert g["databases"][0]["tables"] == ["dbo.orders"]
    assert (g["first_at"], g["last_at"]) == ("2026-10-23T07:00:00+00:00", "2026-10-23T09:00:00+00:00")


def test_error_groups_say_when_the_list_was_cut_short(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import main
    many = [dict(finished_at="2026-10-23T09:00:00+00:00", task_collection="c1", control_id=i, src_database="db1",
                 src_schema="dbo", src_table=f"t{i}", error="Read timed out") for i in range(4)]
    monkeypatch.setattr(main, "ERROR_TABLES_LIMIT", 3)
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): many[:params[1]])
    body = TestClient(main.app).get("/api/errors/groups?hours=5").json()
    assert body["truncated"] is True and body["tables_shown"] == 3
    assert sum(g["tables"] for g in body["groups"]) == 3


def test_a_table_failing_for_two_causes_is_in_both_groups():
    base = dict(task_collection="c1", control_id=7, src_database="db1", src_schema="dbo", src_table="orders")
    rows = [dict(base, finished_at="2026-10-23T09:00:00+00:00", error="Invalid column name 'qty'.", failures=1),
            dict(base, finished_at="2026-10-23T08:00:00+00:00", error="connection reset", failures=1)]
    by_class = {g["error_class"]: g for g in q.group_errors(rows)}
    assert set(by_class) == {"missing", "connection"}
    assert by_class["connection"]["tables"] == by_class["missing"]["tables"] == 1
    assert by_class["connection"]["last_at"] == "2026-10-23T08:00:00+00:00"


def test_failure_groups_keep_two_servers_databases_of_one_name_apart():
    rows = [dict(task_collection="c1", server=s, control_id=i, src_database="Sales", src_schema="dbo", src_table=t,
                 finished_at="2026-10-23T09:00:00+00:00", error="connection reset")
            for i, s, t in ((1, "srvX", "orders"), (2, "srvY", "customers"))]
    g = q.group_errors(rows)[0]
    assert sorted((d["server"], d["tables"][0]) for d in g["databases"]) == [("srvX", "dbo.orders"), ("srvY", "dbo.customers")]


# ---- API (needs FastAPI and httpx, like api_test.py) --------------------------------------------

@pytest.fixture
def api(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import main
    from fastapi.testclient import TestClient

    answers = {
        q.rollup_states_sql(): [
            dict(server="s1", src_connection="sql_b", src_host=None, task_collection="c1", src_database="db1", tables=2,
                 state_loaded=2, late_database=False, state_first_load=0, state_failing=0, no_sla_tables=0),
            dict(server="s1", src_connection="sql_b", src_host=None, task_collection="c2", src_database="db1", tables=2,
                 state_loaded=2, late_database=False, state_first_load=0, state_failing=0, no_sla_tables=0),
            dict(server="s1", src_connection=None, src_host="other", task_collection="c1", src_database="db2", tables=6,
                 state_loaded=6, late_database=False, state_first_load=0, state_failing=0, no_sla_tables=0)],
        q.rollup_group_names_sql(): [dict(task_collection="c1"), dict(task_collection="c2")],
        q.control_group_names_sql(): [dict(task_collection="c1"), dict(task_collection="c2")],
        q.last_runs_sql(): [dict(task_collection="c1", run_id=9, status="running")],
        q.running_progress_sql(): [dict(run_id=9, task_collection="c1", enabled_tables=10, tables_checked=4)],
        q.run_batch_rows_sql(): [dict(run_id=8, task_collection="c1", detail=json.dumps({"batch_rows": 50}))],
        q.rows_written_since_sql(): [dict(rows_written=700, uncounted=2)],
        q.collection_databases_sql(): [dict(server="s1", src_database="db1", tables=5), dict(server="s1", src_database="db2", tables=5)],
        q.collection_database_rows_sql(): [dict(server="s1", src_database="db1", rows_first_loads=1000, first_loads=5,
                                                first_loads_uncounted=0, rows_changes_24h=3, in_flight=1,
                                                failed_this_run=0)],
        q.collection_batch_runs_sql(): [dict(run_id=7, detail=json.dumps({"batch_rows": 400, "batch_rows_unknown": 0}),
                                             databases=["db2"], keys=[q.server_database_key("s1", "db2")])],
        # Run 8's three loads were members of its batched append, so none of them counts as uncounted.
        q.run_work_sql(): [dict(run_id=9, rows_written=12, uncounted=1), dict(run_id=8, rows_written=None, uncounted=0)],
        q.recent_errors_sql(): [dict(finished_at="2026-10-23T07:40:00+00:00", task_collection="c1", src_database="db1",
                                     src_schema="dbo", src_table="t", error="connection reset")],
        q.failed_tables_sql(): [dict(finished_at="2026-10-23T07:40:00+00:00", task_collection="c1", control_id=1, src_database="db1",
                                     src_schema="dbo", src_table="t", error="connection reset")],
    }

    def query(sql, params=()):
        if sql.startswith("select run_id, task_collection, status, started_at"):      # runs_sql
            return [dict(run_id=9, task_collection="c1", status="running", detail=None),
                    dict(run_id=8, task_collection="c1", status="completed",
                         detail=json.dumps({"batch_rows": 50, "batch_rows_unknown": 0}))]
        return answers.get(sql, [])

    class Connection:
        def __init__(self, name, kind, host):
            self.name, self.connection_type, self.options = name, kind, {"host": host, "port": "1433"}
            self.owner, self.comment = "owner", None

    class Workspace:
        class connections:
            @staticmethod
            def list():
                return [Connection("sql_b", "SQLSERVER", "h2"), Connection("pg_a", "POSTGRESQL", "h1")]

    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "workspace", lambda: Workspace())
    monkeypatch.setattr(main, "MONITOR_CACHE_S", 0)
    return TestClient(main.app)


def test_overview_sums_the_shared_reads_into_jobs_and_groups_in_no_job(api):
    data = api.get("/api/overview").json()
    assert data["jobs"] == [] and data["not_in_job"]["groups"] == ["c1", "c2"]
    assert (data["not_in_job"]["enabled_tables"], data["not_in_job"]["databases"]) == (10, 2)
    assert [r["run_id"] for r in data["running_not_in_job"]] == [9]
    assert data["totals"]["enabled_tables"] == 10 and data["totals"]["servers"] == 1
    # 24 hours of rows: the table loads' and the batched appends' together, partial since two loads had no count.
    assert data["rows_24h"] == {"rows": 750, "partial": True}
    assert data["next_triggers"] == [] and data["paused_jobs"] == 0     # no job yet


def test_collection_databases_merge_rows_and_place_a_batch_on_its_one_database(api):
    data = api.get("/api/collections/c1/databases").json()
    by_name = {d["src_database"]: d for d in data["databases"]}
    assert by_name["db1"]["rows_first_loads"] == 1000 and by_name["db1"]["in_flight"] == 1
    assert by_name["db2"]["rows_first_loads"] is None and by_name["db2"]["batches"] == [{"run_id": 7, "rows": 400}]
    assert data["running"]["run_id"] == 9


def test_runs_report_rows_written_with_batches_and_say_when_partial(api):
    runs = {r["run_id"]: r for r in api.get("/api/runs").json()}
    assert runs[9]["rows_written"] == 12 and runs[9]["rows_partial"] is True
    assert runs[8]["rows_written"] == 50 and runs[8]["rows_partial"] is False


def test_a_run_with_a_batch_and_an_uncounted_full_reload_is_partial(monkeypatch):
    import main
    work = {q.run_work_sql(): [dict(run_id=1, rows_written=None, uncounted=1)]}
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): work.get(sql, []))
    run = dict(run_id=1, status="ok", ended_without_finishing=False,
               detail=json.dumps({"batch_rows": 50, "batch_rows_unknown": 0}))
    done = main._with_rows_written([run])[0]
    assert done["rows_written"] == 50 and done["rows_partial"] is True


def test_loads_counted_by_a_batch_are_not_uncounted():
    for sql in (q.run_work_sql(), q.rows_written_since_sql()):
        assert "coalesce(e.pool, '') not like 'batch%%'" in sql


def test_error_groups_endpoint(api):
    body = api.get("/api/errors/groups?hours=24").json()
    groups = body["groups"]
    assert groups[0]["error_class"] == "connection" and groups[0]["tables"] == 1
    assert (body["tables_shown"], body["truncated"]) == (1, False)


def test_connections_list_sorted_with_tracked_counts_and_discoverable_flag(api):
    data = api.get("/api/connections").json()
    assert [c["name"] for c in data["connections"]] == ["pg_a", "sql_b"]
    sql_b = data["connections"][1]
    assert sql_b["discoverable"] is True and data["connections"][0]["discoverable"] is False
    assert sql_b["tracked"] == {"databases": 1, "tables": 4, "collections": 2}
    assert api.get("/api/connections/sql_b/databases").json()[0]["src_database"] == "db1"
    assert api.get("/api/connections/nope/databases").status_code == 404


def test_refresh_lists_a_connection_made_since_the_shared_read(api, monkeypatch):
    import main

    monkeypatch.setattr(main, "MONITOR_CACHE_S", 60)
    made = [{"name": "sql_b", "connection_type": "SQLSERVER", "host": "h", "port": "1433", "discoverable": True}]
    monkeypatch.setattr(main, "_connections", lambda: list(made))
    main._shared_reads.pop("connections", None)
    assert [c["name"] for c in api.get("/api/connections").json()["connections"]] == ["sql_b"]
    made.append(dict(made[0], name="sql_new"))
    assert [c["name"] for c in api.get("/api/connections").json()["connections"]] == ["sql_b"]
    assert [c["name"] for c in api.get("/api/connections?fresh=true").json()["connections"]] == ["sql_b", "sql_new"]
    main._shared_reads.pop("connections", None)


def test_connections_listing_error_is_returned_not_raised(api, monkeypatch):
    import main

    def refuse():
        raise PermissionError("no USE CONNECTION")
    monkeypatch.setattr(main, "_connections", refuse)
    data = api.get("/api/connections").json()
    assert data["connections"] == [] and "USE CONNECTION" in data["error"]
