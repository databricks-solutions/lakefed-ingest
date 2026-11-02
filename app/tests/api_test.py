"""FastAPI backend (main.py) with a fake database: bulk register merge / dry run, validation, SPA.

Skipped when FastAPI isn't installed (the repo's base test environment needs only pytest);
run with the app's requirements installed: `pip install -r app/src/requirements.txt httpx pytest`."""
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

DISCOVERED = [
    dict(discovery_id="d1", src_database="db1", src_schema="dbo", src_table="orders", row_count=10, size_mb=2048.0,
         ct_enabled=True, primary_key="id", select_list="id, v", excluded_columns=None, rowversion_column=None,
         identity_column=None, load_type="ct", cursor_column=None, partition_candidate="id", notes=None,
         src_type="sqlserver", src_host="h", src_port=None, secret_key="k-db1", registered_id=None),
    dict(discovery_id="d1", src_database="db1", src_schema="dbo", src_table="lines", row_count=5, size_mb=1.0,
         ct_enabled=False, primary_key=None, select_list="a, b", excluded_columns=None, rowversion_column=None,
         identity_column=None, load_type="full", cursor_column=None, partition_candidate=None, notes=None,
         src_type="sqlserver", src_host="h", src_port=None, secret_key="k-db1", registered_id=None),
]


CONTROL = {
    7: dict(id=7, task_collection="c1", task_enabled=True, tier=None, priority=None, src_type="sqlserver", src_host="h",
            src_port=None, secret_key="k", src_database="db1", src_schema="dbo", src_table="orders", select_list="id, v",
            primary_key="id", load_type="ct", cursor_column=None, sink_catalog="main", sink_schema="bronze",
            sink_table="orders", client_id=None, load_partitioned=False, partition_col=None, partition_size_mb=None,
            jdbc_options=None),
}
CONTROL[8] = dict(CONTROL[7], id=8, src_table="lines", sink_table="lines")


class FakeCursor:
    """Just enough of a psycopg cursor for main.py's transactional paths."""
    def __init__(self, db):
        self.db, self.description, self._rows, self.rowcount = db, None, [], 0
        self.connection = type("C", (), {"rollback": lambda s: db.rolled_back.append(True)})()

    def execute(self, sql, params=()):
        self._execute(sql, params)

    def _execute(self, sql, params, row_of_batch=False):
        self._rows, self.description = [], None
        # A batched insert (several rows in one statement) is recorded as one statement per row, as it was written
        # before batching, so the tests read each row's params alike.
        width = (len(q.CONTROL_INSERT_COLUMNS) if sql.startswith("insert into lakefed_ingest.control (")
                 else 9 if sql.startswith("insert into lakefed_ingest.control_change") else 0)
        if width and len(params) > width:
            self.db.batch_rows.clear()
            for at in range(0, len(params), width):
                self._execute(sql, params[at:at + width], row_of_batch=True)
            self._rows = list(self.db.batch_rows)
            return
        if not row_of_batch:
            self.db.batch_rows.clear()
        self.db.statements.append((sql, params))
        if "for update" in sql:
            ids = params[0]
            rows = [CONTROL[i] for i in sorted(ids) if i in CONTROL]
            cols = list(next(iter(CONTROL.values())).keys())
            self.description = [type("D", (), {"name": c})() for c in cols]
            self._rows = [tuple(r[c] for c in cols) for r in rows]
        elif sql == q.collection_locks_sql():
            self._rows = [(key, key not in self.db.running) for key in params[0]]
        elif "pg_try_advisory_xact_lock" in sql:
            self._rows = [(params[0] not in self.db.running,)]
        elif sql == q.mixed_sinks_for_sql():
            sinks = [f"{c}.{sc}.{t}" for c, sc, t in zip(*params)]
            self._rows = [(s,) for s in sinks if s in self.db.mixed]
        elif sql == q.split_databases_sql():
            self._rows = [(k, k.split("\x1f")[1], ["c1", "c2"]) for k in params[0] if k in self.db.split]
        elif sql.startswith("insert into lakefed_ingest.control ("):
            if self.db.unique_violation:
                import psycopg
                raise psycopg.errors.UniqueViolation("duplicate")
            table = params[q.CONTROL_INSERT_COLUMNS.index("src_table")]
            control_id = 100 + len(self.db.statements)
            if "returning id, lower(" in sql:     # q.insert_controls_sql: the id with the row's sink key
                self._rows = [] if table == "lines" else [(control_id, *q.control_sink_key(params))]
                self.db.batch_rows += self._rows
            else:
                self._rows = [] if table == "lines" else [(control_id,)]
        elif sql.startswith("update lakefed_ingest.control set") and self.db.unique_violation:
            import psycopg
            raise psycopg.errors.UniqueViolation("duplicate")
        self.rowcount = 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self):
        self.statements, self.rolled_back, self.running, self.unique_violation = [], [], set(), False
        self.batch_rows = []        # what a batched insert's rows return, gathered row by row
        self.mixed = set()          # sinks the mixed-sink check reports
        self.split = set()          # databases (q.database_key) the one-collection check reports split

    def of(self, prefix):
        return [p for sql, p in self.statements if sql.startswith(prefix)]


@pytest.fixture
def client(monkeypatch):
    import contextlib
    fake = FakeDB()
    # These tests are about the writes; that every group belongs to a job is tested in group_rule_test.py, and that
    # only an app manager reads by host outside a connection in host_reads_test.py.
    monkeypatch.setattr(main, "_refuse_groups_outside_jobs", lambda groups: None)
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): DISCOVERED if "discovered_table" in sql else [])
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield FakeCursor(fake))))
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db = fake
    return c


def test_review_hides_server_fields_and_partitions_large_tables(client):
    rows = client.post("/api/discoveries/d1/review", json={"shared": {"sink_schema": "bronze"}}).json()
    assert {r["src_table"]: r["load_partitioned"] for r in rows} == {"orders": True, "lines": False}
    assert not any(k.startswith("_") for r in rows for k in r)


def test_register_dry_run_inserts_nothing(client):
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "dry_run": True}).json()
    assert r["counts"] == {"valid": 2} and client.db.of("insert into lakefed_ingest.control (") == []


def test_register_applies_only_editable_fields_and_reports_skips(client):
    edits = [{"src_database": "db1", "src_schema": "dbo", "src_table": "orders", "include": True,
              "sink_table": "ord", "secret_key": "stolen", "_secret_key": "stolen"}]
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "edits": edits}).json()
    assert r["counts"] == {"registered": 1, "skipped": 1}
    inserted = [dict(zip(q.CONTROL_INSERT_COLUMNS, p)) for p in client.db.of("insert into lakefed_ingest.control (")]
    orders = next(i for i in inserted if i["src_table"] == "orders")
    assert orders["sink_table"] == "ord" and orders["secret_key"] == "k-db1" and orders["load_partitioned"] is True
    assert [p[0:2] for p in client.db.of("insert into lakefed_ingest.control_change")] == [("op@example.com", "register")]


def test_register_requires_collection_and_reports_invalid_rows(client):
    assert client.post("/api/discoveries/d1/register", json={"task_collection": ""}).status_code == 422
    edits = [{"src_database": "db1", "src_schema": "dbo", "src_table": "orders", "load_type": "cursor"}]
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "edits": edits, "dry_run": True}).json()
    assert r["counts"] == {"invalid": 1, "valid": 1}
    assert "cursor_column is required for load_type cursor" in r["results"][0]["errors"]


def test_add_table_validation_error_is_422(client):
    r = client.post("/api/tables", json={"task_collection": "c"})
    assert r.status_code == 422 and "src_host is required" in r.json()["detail"]["errors"]


def test_bad_status_filter_is_400_and_unknown_api_is_404(client):
    assert client.get("/api/tables?status=bogus").status_code == 400
    assert client.get("/api/nope").status_code == 404



# ---- editing ----------------------------------------------------------------------------------------

def _edit(client, ids, changes, dry_run=False):
    return client.post("/api/tables/edit", json={"ids": ids, "changes": changes, "dry_run": dry_run})


def test_edit_preview_writes_nothing(client):
    r = _edit(client, [7], {"sink_table": "orders2"}, dry_run=True).json()
    assert r["summary"]["applied"] is False and r["plans"][0]["reseed"] == "sink_changed"
    assert client.db.of("update lakefed_ingest.control set") == []


def test_sink_edit_flips_to_seeding_and_logs(client):
    r = _edit(client, [7], {"sink_table": "orders2"}).json()
    assert r["summary"] == {"rows": 1, "changed": 1, "reseed": 1, "errors": 0, "applied": True}
    assert client.db.of("update lakefed_ingest.control set sink_table = %s") == [["orders2", 7]]
    assert client.db.of("update lakefed_ingest.checkpoint set phase = 'seeding'") == [("reseed after edit: sink_changed", [7])]
    log = client.db.of("insert into lakefed_ingest.control_change")
    assert log[0][:8] == ("op@example.com", "edit", 7, "c1", "sink_table", "orders", "orders2", "sink_changed")


def test_bulk_move_takes_both_collection_locks_and_does_not_reseed(client):
    r = _edit(client, [7, 8], {"task_collection": "c2"}).json()
    assert r["summary"]["applied"] and r["summary"]["reseed"] == 0
    locks = [p[0] for p in client.db.of("select pg_try_advisory_xact_lock")]
    assert locks == ["lakefed_ingest:c1", "lakefed_ingest:c2"]
    assert client.db.of("update lakefed_ingest.checkpoint") == []


def test_edit_refused_while_collection_runs(client):
    client.db.running = {"lakefed_ingest:c2"}
    r = _edit(client, [7], {"task_collection": "c2"})
    detail = r.json()["detail"]
    assert r.status_code == 409 and "its ingestion job is running (group 'c2')" in detail["message"]
    assert detail["reason"] == "running" and detail["group"] == "c2"


def test_bulk_edit_rejects_single_row_fields_and_invalid_rows(client):
    r = _edit(client, [7, 8], {"primary_key": "id, v"}).json()
    assert r["summary"]["applied"] is False and "cannot be changed in bulk: primary_key" in r["plans"][0]["errors"][0]
    r = _edit(client, [7], {"load_type": "cursor"}).json()
    assert r["summary"]["applied"] is False and "cursor_column is required for load_type cursor" in r["plans"][0]["errors"]
    assert client.db.of("update lakefed_ingest.control set") == []


def test_edit_sink_conflict_is_409_and_nothing_changes(client):
    client.db.unique_violation = True
    r = _edit(client, [7], {"sink_table": "lines"})
    assert r.status_code == 409 and "already writes that sink" in r.json()["detail"]


def test_enable_and_reseed_are_locked_and_logged(client):
    assert client.post("/api/tables/enable", json={"ids": [7], "enabled": False}).json() == {"updated": 1}
    assert client.post("/api/tables/reseed", json={"ids": [7, 8]}).status_code == 200
    actions = [p[1] for p in client.db.of("insert into lakefed_ingest.control_change")]
    assert actions == ["disable", "reseed", "reseed"]
    client.db.running = {"lakefed_ingest:c1"}
    assert client.post("/api/tables/reseed", json={"ids": [7]}).status_code == 409


def test_reseed_counts_the_checkpoints_it_flipped_not_the_rollups_written_after_it(client, monkeypatch):
    """The count is the reseed statement's: the rollup rewrites after it run on the same cursor, and a cursor's rowcount
    is its last statement's."""
    class Cursor(FakeCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql == q.force_reseed_sql():
                self.rowcount = len(params[0])
            elif sql == q.control_databases_sql("ids"):
                self._rows = [("c1", "db1")]
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor(client.db))))
    assert client.post("/api/tables/reseed", json={"ids": [7, 8]}).json() == {"updated": 2}


# ---- review findings: static files, inserts, discovery runs ----------------------------------------

def test_static_files_never_leave_the_static_folder(client):
    for path in ("/%2e%2e/main.py", "/%2e%2e/%2e%2e/README.md", "/..%2fmain.py"):
        r = client.get(path)
        assert "lakefed_ingest operations app" not in r.text and "fastapi" not in r.text.lower()[:200], path


ROW = dict(task_collection="c1", src_type="sqlserver", src_host="h", secret_key="k", src_database="db1", src_schema="dbo",
           src_table="t", select_list="id, v", primary_key="id", load_type="ct", sink_catalog="main", sink_schema="bronze",
           sink_table="orders", client_id="db1")


def test_add_table_takes_the_run_lock_and_refuses_a_mixed_sink(client):
    assert client.post("/api/tables", json=ROW).status_code == 200
    assert [p[0] for p in client.db.of("select pg_try_advisory_xact_lock")] == ["lakefed_ingest:c1"]
    client.db.running = {"lakefed_ingest:c1"}
    assert client.post("/api/tables", json=ROW).status_code == 409
    client.db.running, client.db.mixed = set(), {"main.bronze.orders"}
    r = client.post("/api/tables", json=ROW)
    assert r.status_code == 409 and "claimed by a dedicated row and by other rows" in r.json()["detail"]


def test_register_takes_the_run_lock_and_refuses_a_mixed_sink(client):
    body = {"task_collection": "c9", "shared": {"sink_schema": "bronze"}}
    assert client.post("/api/discoveries/d1/register", json=body).status_code == 200
    assert [p[0] for p in client.db.of("select pg_try_advisory_xact_lock")] == ["lakefed_ingest:c9"]
    client.db.mixed = {"main.bronze.orders"}
    r = client.post("/api/discoveries/d1/register", json=body)
    assert r.status_code == 409 and "main.bronze.orders" in r.json()["detail"]


def test_discovery_run_state_only_for_discovery_runs(client, monkeypatch):
    monkeypatch.setattr(main.jobs, "DISCOVER_JOB_ID", "55")
    monkeypatch.setattr(main.jobs, "run_state", lambda rid: {"run_id": rid, "job_id": 55 if rid == 1 else 99,
                                                              "state": "TERMINATED", "result": "FAILED", "message": "m", "url": "u"})
    assert client.get("/api/discoveries/runs/1").json()["result"] == "FAILED"
    assert client.get("/api/discoveries/runs/2").status_code == 404


def test_connections_lists_only_database_connections_lakefed_can_read(monkeypatch):
    from types import SimpleNamespace
    kind = lambda v: SimpleNamespace(value=v)
    listed = [SimpleNamespace(name=n, connection_type=k, options={"host": f"{n}.example", "port": "1"}, owner="o", comment=None)
              for n, k in (("z_sql", kind("SQLSERVER")), ("mcp_slack", kind("HTTP")), ("pg", kind("POSTGRESQL")),
                           ("ora", "ORACLE"), ("my", kind("MYSQL")), ("snow", kind("SNOWFLAKE")), ("gdrive", kind("HTTP")))]
    monkeypatch.setattr(main.db, "workspace", lambda: SimpleNamespace(connections=SimpleNamespace(list=lambda: listed)))
    found = main._connections()
    assert [c["name"] for c in found] == ["my", "ora", "pg", "z_sql"]
    assert {c["name"]: c["discoverable"] for c in found} == {"my": False, "ora": False, "pg": False, "z_sql": True}


def test_run_detail_returns_the_run_its_rows_and_live_progress(monkeypatch):
    run = dict(run_id=5, task_collection="c1", status="running", total=10, ok=4, failed=1, skipped=2,
               started_at="2026-10-26T13:00:05+00:00")
    calls = []

    def query(sql, params=()):
        calls.append(sql)
        if sql == q.run_sql():
            return [dict(run)] if params == (5,) else []
        if sql == q.run_work_sql():
            return [dict(run_id=5, rows_written=120, uncounted=0)]
        if sql == q.running_progress_sql():
            return [dict(run_id=4, tables_ok=9), dict(run_id=5, tables_ok=4, tables_running=3, enabled_tables=10)]
        if sql == q.schedules_sql():
            return []
        if sql == q.first_started_sql():
            assert params == ("check-77", "check-77:%")
            return [dict(task_collection="c1", started_at=run["started_at"])]
        raise AssertionError(sql)
    monkeypatch.setattr(main.db, "query", query)
    client = TestClient(main.app)
    body = client.get("/api/runs/5").json()
    assert body["run_id"] == 5 and body["rows_written"] == 120 and body["progress"]["tables_running"] == 3
    assert body["check"] is None
    assert client.get("/api/runs/6").status_code == 404
    run.update(status="completed", work_id="check-77",
               detail=json.dumps({"check": {"work": [], "unchanged_tables": 10}}))
    assert client.get("/api/runs/5").json()["check"] == {
        "run_id": 5, "task_collection": "c1", "status": "completed", "unchanged_tables": 10, "databases_with_work": 0,
        "found": "Checked: no changes", "error": None}


def test_a_collections_databases_keep_two_servers_databases_of_one_name_apart(monkeypatch):
    states = [dict(server="srvX", src_database="Sales", tables=3), dict(server="srvY", src_database="Sales", tables=3)]
    written = [dict(server="srvX", src_database="Sales", rows_first_loads=450, first_loads=3),
               dict(server="srvY", src_database="Sales", rows_first_loads=600, first_loads=3)]
    batches = [dict(run_id=9, detail='{"batch_rows": 70}', databases=["Sales"], keys=[q.server_database_key("srvY", "Sales")])]

    def query(sql, params=()):
        return {q.collection_database_rows_sql(): written, q.running_progress_sql(): [], q.collection_batch_runs_sql(): batches,
                q.collection_databases_sql(): states, q.schedules_sql(): []}[sql]
    monkeypatch.setattr(main.db, "query", query)
    body = TestClient(main.app).get("/api/collections/two_servers_one_name/databases").json()
    got = {(d["server"], d["src_database"]): (d["rows_first_loads"], d["batches"]) for d in body["databases"]}
    assert got == {("srvX", "Sales"): (450, []), ("srvY", "Sales"): (600, [{"run_id": 9, "rows": 70}])}


def test_run_pages_share_one_read_of_running_progress(monkeypatch):
    reads = []

    def query(sql, params=()):
        if sql == q.run_sql():
            return [dict(run_id=params[0], task_collection="c1", status="running", detail=None)]
        if sql == q.running_progress_sql():
            reads.append(sql)
            return [dict(run_id=5, tables_ok=4, enabled_tables=10), dict(run_id=6, tables_ok=7, enabled_tables=10)]
        return []
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main, "MONITOR_CACHE_S", 15)
    client = TestClient(main.app)
    assert client.get("/api/runs/5").json()["progress"]["tables_ok"] == 4
    assert client.get("/api/runs/6").json()["progress"]["tables_ok"] == 7
    assert len(reads) == 1


def test_the_overviews_job_reads_refresh_as_often_as_the_running_runs(monkeypatch):
    reads, clock = [], [1000.0]

    def query(sql, params=()):
        reads.append(sql)
        return []
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main, "MONITOR_CACHE_S", 15)
    monkeypatch.setattr(main, "STATES_CACHE_S", 60)
    client = TestClient(main.app)
    client.get("/api/overview")
    clock[0] += 20
    client.get("/api/overview")
    assert reads.count(q.running_progress_sql()) == reads.count(q.schedules_sql()) == 2
    assert reads.count(q.rollup_states_sql()) == 1


def test_the_overview_says_when_its_table_states_were_read(monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [])
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main, "STATES_CACHE_S", 60)
    client = TestClient(main.app)
    first = client.get("/api/overview").json()
    clock[0] += 30
    again = client.get("/api/overview").json()
    assert again["read_at"] == first["read_at"] and again["states_cache_s"] == 60


def test_runs_carry_their_outcome_and_speed_and_a_closed_run_has_no_duration(monkeypatch):
    start = datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)
    listed = [dict(run_id=8, task_collection="c1", status="failed", failed=0, duration_s=5000,
                   detail=json.dumps({"closed_by_run": 9, "reason": "x"})),
              dict(run_id=7, task_collection="c1", status="failed", failed=2, duration_s=600, detail=None)]

    def query(sql, params=()):
        if sql.startswith("select run_id, task_collection, status, started_at"):
            return [dict(r) for r in listed]
        if sql == q.run_work_sql():
            return [dict(run_id=7, loaded=30, work_started_at=start, work_finished_at=start + timedelta(minutes=3)),
                    dict(run_id=8, loaded=1, work_started_at=start, work_finished_at=start + timedelta(hours=12))]
        return []
    monkeypatch.setattr(main.db, "query", query)
    got = {r["run_id"]: r for r in TestClient(main.app).get("/api/runs").json()}
    assert got[8]["outcome"]["label"] == "Ended without finishing" and got[8]["duration_s"] is None
    assert got[7]["outcome"]["label"] == "Succeeded with failures" and got[7]["duration_s"] == 600
    assert got[7]["speed_tables_per_min"] == 10.0 and got[8]["speed_tables_per_min"] is None


def test_a_running_run_shows_what_it_is_doing_and_about_how_long_is_left(monkeypatch):
    def query(sql, params=()):
        if sql == q.run_sql():
            return [dict(run_id=5, task_collection="c1", status="running", detail=None)]
        if sql == q.running_progress_sql():
            return [dict(run_id=5, enabled_tables=10, elapsed_s=60, p50_s=600, work_started_at=None)]
        return []
    monkeypatch.setattr(main.db, "query", query)
    body = TestClient(main.app).get("/api/runs/5").json()
    assert body["outcome"]["label"] == "Detecting changes"
    assert body["time_left"] == {"seconds": 540, "basis": "typical"} and body["live_speed"] is None


def test_the_in_flight_list_says_how_many_it_shows(monkeypatch):
    page = [dict(src_table=f"t{i}", running_s=90) for i in range(main.IN_FLIGHT_LIMIT)]
    answers = {5: (page, 64), 6: (page[:3], None)}

    def query(sql, params=()):
        listed, total = answers[params[0]]
        if sql == q.in_flight_sql():
            assert params[1] == main.IN_FLIGHT_LIMIT
            return listed
        if sql == q.in_flight_count_sql() and total is not None:
            return [dict(tables=total)]
        raise AssertionError(sql)
    monkeypatch.setattr(main.db, "query", query)
    client = TestClient(main.app)
    assert client.get("/api/runs/5/in-flight").json() == {"tables": page, "total": 64, "limit": main.IN_FLIGHT_LIMIT}
    assert client.get("/api/runs/6/in-flight").json()["total"] == 3


def test_a_running_run_in_the_list_says_what_it_is_doing(monkeypatch):
    def query(sql, params=()):
        if sql.startswith("select run_id, task_collection, status, started_at"):
            return [dict(run_id=5, task_collection="c1", status="running", detail=None)]
        if sql == q.running_progress_sql():
            return [dict(run_id=5, tables_ok=3, enabled_tables=10)]
        return []
    monkeypatch.setattr(main.db, "query", query)
    assert TestClient(main.app).get("/api/runs").json()[0]["outcome"]["label"] == "Loading changed tables"


def test_tables_come_a_page_at_a_time_with_the_cursor_of_the_next(monkeypatch):
    seen, available = [], [main.TABLES_PAGE + 1]

    def query(sql, params=()):
        seen.append(params)
        return [dict(id=i, task_collection="c1", src_database="db1", src_schema="dbo", src_table=f"t{i}")
                for i in range(min(params[-1], available[0]))]
    monkeypatch.setattr(main.db, "query", query)
    client = TestClient(main.app)
    body = client.get("/api/tables?collection=c1&database=db1").json()
    last = main.TABLES_PAGE - 1
    assert len(body["tables"]) == main.TABLES_PAGE and body["next_after"] == ["c1", "db1", "dbo", f"t{last}", last]
    client.get("/api/tables", params={"after": json.dumps(body["next_after"])})
    assert seen[-1][:-1] == tuple(body["next_after"])
    available[0] = main.TABLES_PAGE      # exactly one page: no Next
    assert client.get("/api/tables").json()["next_after"] is None




# ---- a database's tables share one collection ------------------------------------------------------

def test_adding_a_table_that_would_put_its_database_in_a_second_collection_is_refused(client):
    assert client.post("/api/tables", json=ROW).status_code == 200
    assert client.db.of(q.split_databases_sql()) == [([q.database_key(ROW)],)]
    client.db.split = {q.database_key(ROW)}
    r = client.post("/api/tables", json=ROW)
    assert r.status_code == 409 and "db1" in r.json()["detail"]["message"] and "c1, c2" in r.json()["detail"]["message"]


def test_moving_some_of_a_databases_tables_to_another_collection_is_refused(client):
    client.db.split = {q.database_key(CONTROL[7])}
    r = client.post("/api/tables/edit", json={"ids": [7], "changes": {"task_collection": "c2"}, "dry_run": False})
    assert r.status_code == 409 and "more than one ingestion group" in r.json()["detail"]["message"]


def test_registering_tables_into_another_collection_than_their_database_is_refused(client):
    body = {"task_collection": "c9", "shared": {"sink_schema": "bronze"}}
    client.db.split = {q.database_key(dict(DISCOVERED[0], task_collection="c9"))}
    r = client.post("/api/discoveries/d1/register", json=body)
    assert r.status_code == 409 and "db1" in r.json()["detail"]["message"]



def test_a_databricks_run_link_opens_its_run_page(monkeypatch):
    monkeypatch.setattr(main.jobs, "run_state", lambda run_id: {"url": f"https://h/jobs/1/runs/{run_id}"})
    r = TestClient(main.app).get("/api/databricks-runs/42/open", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://h/jobs/1/runs/42"
    monkeypatch.setattr(main.jobs, "run_state", lambda run_id: (_ for _ in ()).throw(RuntimeError("Run 43 does not exist. More")))
    assert TestClient(main.app).get("/api/databricks-runs/43/open", follow_redirects=False).status_code == 404
