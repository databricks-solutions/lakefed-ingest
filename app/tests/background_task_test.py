"""Large writes: control rows batched q.CONTROL_BATCH to a statement, and a write of more than main.BACKGROUND_ROWS
tables run as a background task (app_task) that the request answers 202 with at once. Fake database; the thread is
run inline when a test asks for it."""
import contextlib
import time

import pytest

import queries as q
import table_removal as tr

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from api_test import FakeCursor, FakeDB  # noqa: E402
from new_job_test import FAN_IN  # noqa: E402

HEAD = "src_connection,src_database,src_schema,src_table,select_list,primary_key\n"


def _csv(n: int, taken_at: int = -1) -> str:
    """n tables over 10 databases; the one at taken_at is named lines, whose target the fake says is taken."""
    return HEAD + "".join(f"sql_a,eng_{i % 10:03d},gen,{'lines' if i == taken_at else f't{i:05d}'},\"id, a\",id\n"
                          for i in range(n))


def _row(i: int) -> dict:
    row = {c: None for c in q.CONTROL_INSERT_COLUMNS}
    row.update(task_collection="g1", src_database="db", src_schema="dbo", src_table=f"t{i}", sink_catalog="main",
               sink_schema="s", sink_table=f"t{i}", client_id=None)
    return row


# ---- the batched statements ----------------------------------------------------------------------------------------

def test_a_batched_insert_takes_every_column_of_each_row_and_returns_each_rows_sink_key():
    sql = q.insert_controls_sql(3)
    assert sql.count("%s") == 3 * len(q.CONTROL_INSERT_COLUMNS) == 75
    assert sql.endswith("on conflict do nothing returning id, lower(sink_catalog), lower(sink_schema), "
                        "lower(sink_table), coalesce(client_id, '')")
    # The most parameters one statement carries stays well under Postgres' 65,535.
    assert q.CONTROL_BATCH * len(q.CONTROL_INSERT_COLUMNS) < 65_535
    assert q.log_changes_sql(2).count("%s") == 18 and q.log_changes_sql(2).startswith(q.log_change_sql().split(" values ")[0])
    params = q.control_params({**_row(1), "sink_catalog": "Main", "sink_table": "T1", "client_id": "c1"})
    assert q.control_sink_key(params) == ("main", "s", "t1", "c1")


def test_2500_rows_go_in_three_statements_with_their_change_log_lines_and_progress():
    fake, done = FakeDB(), []
    cur = FakeCursor(fake)
    start = time.perf_counter()
    written = main._insert_controls(cur, [_row(i) for i in range(2500)], "op@example.com", "b1", progress=done.append)
    took = time.perf_counter() - start
    inserts = [p for sql, p in fake.statements if sql.startswith("insert into lakefed_ingest.control (")]
    assert len(written) == 2500 and len(inserts) == 2500 and done == [1000, 2000, 2500]
    logs = fake.of("insert into lakefed_ingest.control_change")
    assert len(logs) == 2500 and {p[1] for p in logs} == {"import"} and {p[8] for p in logs} == {"b1"}
    assert [control_id for _, control_id in written] == [p[2] for p in logs]
    print(f"planned and batched 2,500 rows in {took * 1000:.0f} ms")


def test_a_taken_target_refuses_the_lot_or_is_skipped():
    fake = FakeDB()
    rows = [_row(1), {**_row(2), "src_table": "lines"}, _row(3)]
    with pytest.raises(main.HTTPException) as refused:
        main._insert_controls(FakeCursor(fake), rows, "op", "b")
    assert refused.value.status_code == 409 and "another table already writes main.s.t2" in refused.value.detail
    written = main._insert_controls(FakeCursor(FakeDB()), rows, "op", "b", skip_taken=True)
    assert [row["src_table"] for row, _ in written] == ["t1", "t3"]


def test_a_taken_target_of_a_consolidated_row_names_its_database_and_server_not_its_client_id():
    row = {"sink_catalog": "main", "sink_schema": "eng", "sink_table": "t0000", "client_id": "sql_a_eng_000",
           "src_database": "eng_000", "src_connection": "sql_a"}
    assert main._taken_target(row) == "another table already writes main.eng.t0000 for database eng_000 on sql_a"
    assert main._taken_target({**row, "src_connection": None, "src_host": "db.example.net"}) == (
        "another table already writes main.eng.t0000 for database eng_000 on db.example.net")
    assert main._taken_target({**row, "client_id": None}) == "another table already writes main.eng.t0000"


# ---- the background task -------------------------------------------------------------------------------------------

class Tasks:
    """app_task, faked: what the endpoints record, read, beat and finish."""
    def __init__(self):
        self.rows, self.started, self.executed = {}, [], []

    def query(self, sql, params=()):
        if sql == q.collections_sql():
            return [{"task_collection": "other"}]
        if sql == q.insert_task_sql():
            kind, request_id, job_id, total, result, user = params
            for row in self.rows.values():
                if row["request_id"] == request_id:
                    return [{"id": row["id"], "made": False}]
            task_id = len(self.rows) + 1
            self.rows[task_id] = dict(id=task_id, kind=kind, request_id=request_id, job_id=job_id, status="running",
                                      done=0, total=total, result=None, error=None, created_by=user)
            return [{"id": task_id, "made": True}]
        if sql == q.task_of_request_sql():
            return [{"id": row["id"]} for row in self.rows.values() if row["request_id"] == params[0]]
        if sql == q.resume_synced_task_sql():
            row = self.rows.get(params[0])
            if row and row["status"] == "failed" and row["result"] is not None:
                row.update(status="running", error=None)
                return [{"id": row["id"]}]
            return []
        if sql == q.task_sql():
            return [dict(self.rows[params[0]])] if params[0] in self.rows else []
        return []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql == q.finish_task_sql():
            status, job_id, result, error, task_id = params
            import json
            row = self.rows[task_id]
            if row["status"] == "running":
                row.update(status=status, job_id=job_id or row["job_id"], error=error,
                           result=json.loads(result) if result else row["result"])
        elif sql == q.task_progress_sql():
            self.rows[params[1]]["done"] = max(self.rows[params[1]]["done"], params[0])     # greatest(done, %s)

    def run(self):
        for work in self.started:
            work()
        self.started.clear()


@pytest.fixture
def client(monkeypatch):
    fake, tasks = FakeDB(), Tasks()

    class Cursor(FakeCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql == q.insert_schedule_sql():
                self._rows = [(7,)]

    monkeypatch.setattr(main.db, "query", tasks.query)
    monkeypatch.setattr(main.db, "execute", tasks.execute)
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor(fake))))
    monkeypatch.setattr(main.jobs, "schedule_variants", lambda: {"default": "1", "check": "2"})
    monkeypatch.setattr(main, "_start_background", tasks.started.append)
    monkeypatch.setattr(main, "BACKGROUND_ROWS", 50)
    monkeypatch.setattr(main, "TASK_BEAT_S", 3600)
    synced = []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or [])
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db, c.tasks, c.synced = fake, tasks, synced
    return c


def _create(client, n, request_id="r1", **extra):
    return client.post("/api/ingestion-jobs", json={"name": "Big job", "csv": _csv(n, **extra), "settings": FAN_IN,
                                                    "dry_run": False, "request_id": request_id})


def test_a_small_create_is_written_in_the_request_under_its_own_task(client):
    r = _create(client, 20)
    assert r.status_code == 200 and r.json()["created"] is True and r.json()["id"] == 7
    assert client.synced == [7] and client.tasks.started == []
    (task,) = client.tasks.rows.values()
    assert task["status"] == "succeeded" and task["job_id"] == 7 and task["request_id"] == "r1"


def test_a_small_create_refused_is_refused_again_by_its_request_id_without_a_second_attempt(client):
    first = _create(client, 20, request_id="r-refused", taken_at=5)
    assert first.status_code == 409
    statements = len(client.db.statements)
    again = _create(client, 20, request_id="r-refused", taken_at=5)
    assert again.status_code == 202 and again.json()["task"]["status"] == "failed"
    assert "another table already writes" in again.json()["task"]["error"]
    assert len(client.db.statements) == statements


def test_a_create_whose_sync_failed_is_finished_by_sending_its_request_id_again(client, monkeypatch):
    results = [[_sync_error("sql_a", "jobs API down")], []]
    monkeypatch.setattr(main, "_sync", lambda sid, user: client.synced.append(sid) or results.pop(0))
    failed = _create(client, 20, request_id="r-resume")
    assert failed.status_code == 202 and failed.json()["task"]["status"] == "failed"
    assert failed.json()["task"]["result"]["id"] == 7
    inserts = len(client.db.of("insert into lakefed_ingest.control ("))
    done = _create(client, 20, request_id="r-resume")
    assert done.status_code == 202 and done.json()["task"]["status"] == "succeeded"
    assert done.json()["task"]["job_id"] == 7
    assert len(client.db.of("insert into lakefed_ingest.schedule (")) == 1
    assert len(client.db.of("insert into lakefed_ingest.control (")) == inserts == 20
    assert client.synced == [7, 7]


def test_a_large_create_answers_202_with_its_task_then_writes_in_the_background(client):
    r = _create(client, 120)
    assert r.status_code == 202
    task = r.json()["task"]
    assert task["status"] == "running" and task["total"] == 120 and task["kind"] == "create" and task["created_by"] == "op@example.com"
    assert client.db.statements == [] and client.synced == []      # nothing is written before the thread runs
    client.tasks.run()
    done = client.get(f"/api/tasks/{task['id']}").json()
    assert done["status"] == "succeeded" and done["job_id"] == 7 and done["result"]["id"] == 7
    assert done["result"]["created"] is True and done["result"]["summary"]["tables"] == 120
    assert len(client.db.of("insert into lakefed_ingest.control (")) == 120 and client.synced == [7]
    # The task's result is written in the job's own transaction, as its last statement, so they commit together.
    written = [p for sql, p in client.db.statements if sql == q.task_written_sql()]
    assert len(written) == 1 and written[0][1:] == (7, 120, task["id"])
    assert client.db.statements[-1][0] == q.task_written_sql()


def test_the_same_request_id_again_answers_with_the_same_task_and_writes_nothing_more(client):
    first = _create(client, 120).json()["task"]
    again = _create(client, 120)
    assert again.status_code == 202 and again.json()["task"]["id"] == first["id"]
    assert len(client.tasks.started) == 1        # only the first started a thread
    client.tasks.run()
    third = _create(client, 120).json()["task"]
    assert third["id"] == first["id"] and third["status"] == "succeeded" and client.tasks.started == []
    assert len(client.db.of("insert into lakefed_ingest.schedule (")) == 1


def test_a_failed_write_ends_its_task_failed_with_what_the_request_would_have_said(client):
    task = _create(client, 120, taken_at=60).json()["task"]
    client.tasks.run()
    failed = client.get(f"/api/tasks/{task['id']}").json()
    assert failed["status"] == "failed" and "another table already writes" in failed["error"]
    assert failed["result"] is None and client.synced == [] and client.db.rolled_back == []


def test_a_sync_that_fails_after_the_commit_ends_the_task_failed_with_its_result_kept_and_says_why(client, monkeypatch):
    monkeypatch.setattr(main, "_sync", lambda sid, user: (_ for _ in ()).throw(RuntimeError("jobs API down")))
    task = _create(client, 120).json()["task"]
    client.tasks.run()
    done = client.get(f"/api/tasks/{task['id']}").json()
    assert done["status"] == "failed" and done["result"]["after_error"] == "jobs API down"
    assert done["error"].startswith(main.TASK_WRITTEN_NOT_SYNCED) and done["error"].endswith("jobs API down")


def test_a_create_whose_wrapper_sync_failed_is_not_done_though_its_rows_are_written(client, monkeypatch):
    monkeypatch.setattr(main, "_sync", lambda sid, user: [_sync_error("sql_b", "jobs API down")])
    task = _create(client, 120).json()["task"]
    client.tasks.run()
    done = client.get(f"/api/tasks/{task['id']}").json()
    assert done["status"] == "failed" and done["error"] == f"{main.TASK_WRITTEN_NOT_SYNCED} sql_b: jobs API down"
    assert len(client.db.of("insert into lakefed_ingest.control (")) == 120


def test_a_create_whose_own_cluster_copy_did_not_start_is_not_done(client, monkeypatch):
    monkeypatch.setattr(main, "_apply_cluster", lambda sid: "the cluster copies job didn't start: no room")
    monkeypatch.setattr(main, "_refuse_unknown_node_types", lambda cluster: None)
    body = {"name": "Small job", "csv": _csv(10), "settings": FAN_IN, "dry_run": False, "request_id": "r3",
            "run": {"options": {"cluster": {"node_type_id": "i3.xlarge"}}}}
    r = client.post("/api/ingestion-jobs", json=body)
    assert r.status_code == 202
    task = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert task["status"] == "failed" and task["result"]["id"] == 7
    assert task["error"] == f"{main.TASK_WRITTEN_NOT_SYNCED} the cluster copies job didn't start: no room"


def test_progress_is_recorded_by_the_beat_while_the_write_runs(monkeypatch):
    beats, gate = [], []
    monkeypatch.setattr(main, "TASK_BEAT_S", 0.01)
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): beats.append((sql, params)))

    def write(progress, task_id):
        progress(1000)
        deadline = time.time() + 2
        while not any(sql == q.task_progress_sql() and p == (1000, 5) for sql, p in beats) and time.time() < deadline:
            time.sleep(0.01)
        progress(2000)
        gate.append(task_id)
        return {"summary": {}}

    main._run_task(5, write, lambda result: result)
    assert gate == [5] and (q.task_progress_sql(), (1000, 5)) in beats
    assert beats[-1][0] == q.finish_task_sql() and beats[-1][1][0] == "succeeded"


def test_reading_a_task_first_ends_it_when_its_heartbeat_stopped(client):
    task = _create(client, 120).json()["task"]
    client.tasks.executed.clear()
    client.get(f"/api/tasks/{task['id']}")
    stale = [p for sql, p in client.tasks.executed if sql == q.stale_tasks_sql()]
    assert stale == [(main.TASK_UNSYNCED, main.TASK_ROLLED_BACK, main.TASK_PARTIAL, main.TASK_STALE_S, task["id"], None)]
    assert client.get("/api/tasks/999").status_code == 404


def test_the_stale_rule_tells_a_rolled_back_write_from_one_that_committed():
    sql = q.stale_tasks_sql()
    assert "status = 'running' and heartbeat_at < now() - make_interval(secs => %s)" in sql
    assert "set status = 'failed'" in sql and "'succeeded'" not in sql
    # A write that committed some databases and stopped says so: its progress is kept and the same request finishes it.
    assert "when progress is null then %s else %s end" in sql
    assert "status = 'failed' and result is null and progress is not null" in q.resume_task_sql()
    # A late finish can't undo a stale task's verdict, nor a stale read undo a finish.
    assert q.finish_task_sql().endswith("where id = %s and status = 'running'")
    assert q.insert_task_sql().count("%s") == 6 and "on conflict (request_id)" in q.insert_task_sql()


def test_a_heartbeat_never_lowers_the_done_count():
    """The beat writes the progress its thread holds, which can be behind a by-database write's committed count: the
    count only goes up, so an older beat can't take it back."""
    assert "set done = greatest(done, %s)" in q.task_progress_sql()


# ---- Add tables and the databases file -----------------------------------------------------------------------------

@pytest.fixture
def job_client(monkeypatch):
    from new_job_test import _job_rows
    rows, tasks = _job_rows(), Tasks()
    schedule = dict(id=7, name="Engagement extraction", cadence_minutes=60, timezone="UTC", stagger=True, paused=True,
                    options=None, job_variant="default", parameters={}, notify_emails=None)
    members = [dict(id=i, schedule_id=7, task_collection=g, offset_minutes=0, job_id=None, removed_at=None,
                    delete_pending=False) for i, g in enumerate(sorted({r["task_collection"] for r in rows}))]

    def answer(sql, params=()):
        if sql == q.schedule_sql():
            return [dict(schedule)]
        if sql == q.schedule_members_sql():
            return [dict(m) for m in members]
        if sql.startswith(q.groups_control_rows_sql().split(" for update")[0][:60]):
            return [dict(r) for r in rows]
        if sql == q.collections_sql():
            return [{"task_collection": m["task_collection"]} for m in members]
        return tasks.query(sql, params)

    fake = FakeDB()
    monkeypatch.setattr(main.db, "query", answer)
    monkeypatch.setattr(main.db, "execute", tasks.execute)
    monkeypatch.setattr(main.db, "fetch", lambda cur, sql, params=(): answer(sql, params))
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield FakeCursor(fake))))
    monkeypatch.setattr(main, "_refuse_job", lambda job, request, action: None)
    monkeypatch.setattr(main, "_start_background", tasks.started.append)
    monkeypatch.setattr(main, "BACKGROUND_ROWS", 50)
    monkeypatch.setattr(main, "TASK_BEAT_S", 3600)
    synced = []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or [])
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db, c.tasks, c.synced = fake, tasks, synced
    return c


def test_adding_many_tables_runs_as_a_task_of_the_job(job_client):
    text = HEAD + "".join(f"sql_b,ops_{i % 5:02d},gen,t{i:04d},\"id, a\",id\n" for i in range(80))
    r = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": text, "dry_run": False, "request_id": "add-1"})
    assert r.status_code == 202 and r.json()["task"]["job_id"] == 7 and r.json()["task"]["kind"] == "tables"
    job_client.tasks.run()
    done = job_client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert done["status"] == "succeeded" and done["result"]["applied"] is True and done["result"]["summary"]["tables"] == 80
    assert len(job_client.db.of("insert into lakefed_ingest.control (")) == 80 and job_client.synced == [7]


def test_a_job_write_whose_wrapper_sync_failed_is_not_done_and_keeps_its_tables(job_client, monkeypatch):
    monkeypatch.setattr(main, "_sync", lambda sid, user: [_sync_error("sql_b", "jobs API down")])
    text = HEAD + "".join(f"sql_b,ops_{i % 5:02d},gen,t{i:04d},\"id, a\",id\n" for i in range(80))
    r = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": text, "dry_run": False, "request_id": "add-2"})
    job_client.tasks.run()
    done = job_client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert done["status"] == "failed" and done["result"]["applied"] is True
    assert done["error"] == f"{main.TASK_WRITTEN_NOT_SYNCED} sql_b: jobs API down"
    assert len(job_client.db.of("insert into lakefed_ingest.control (")) == 80


def test_a_small_job_write_whose_wrapper_sync_failed_answers_not_done_with_its_task(job_client, monkeypatch):
    monkeypatch.setattr(main, "_sync", lambda sid, user: [_sync_error("sql_b", "jobs API down")])
    text = HEAD + "".join(f"sql_b,ops_{i % 5:02d},gen,t{i:04d},\"id, a\",id\n" for i in range(10))
    r = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": text, "dry_run": False})
    assert r.status_code == 202
    task = r.json()["task"]
    assert task["status"] == "failed" and task["result"]["applied"] is True
    assert task["error"] == f"{main.TASK_WRITTEN_NOT_SYNCED} sql_b: jobs API down"
    assert len(job_client.db.of("insert into lakefed_ingest.control (")) == 10


def test_a_small_write_sent_again_with_its_request_id_answers_its_task_and_writes_nothing_more(job_client):
    text = HEAD + "".join(f"sql_b,ops_{i % 5:02d},gen,t{i:04d},\"id, a\",id\n" for i in range(10))
    body = {"csv": text, "dry_run": False, "request_id": "add-small"}
    first = job_client.post("/api/ingestion-jobs/7/tables", json=body)
    assert first.status_code == 200 and first.json()["applied"] is True
    again = job_client.post("/api/ingestion-jobs/7/tables", json=body)
    assert again.status_code == 202 and again.json()["task"]["status"] == "succeeded"
    assert len(job_client.db.of("insert into lakefed_ingest.control (")) == 10


def test_a_small_write_still_running_under_its_request_id_is_found_running_and_not_written_again(job_client):
    job_client.tasks.query(q.insert_task_sql(), ("tables", "add-running", 7, 10, None, "op@example.com"))
    text = HEAD + "".join(f"sql_b,ops_{i % 5:02d},gen,t{i:04d},\"id, a\",id\n" for i in range(10))
    r = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": text, "dry_run": False, "request_id": "add-running"})
    assert r.status_code == 202 and r.json()["task"]["status"] == "running"
    assert job_client.db.of("insert into lakefed_ingest.control (") == []


def test_a_committed_write_whose_sync_failed_is_finished_by_sending_its_request_id_again(job_client, monkeypatch):
    results, synced = [[_sync_error("sql_b", "jobs API down")], []], []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or results.pop(0))
    text = HEAD + "".join(f"sql_b,ops_{i % 5:02d},gen,t{i:04d},\"id, a\",id\n" for i in range(10))
    body = {"csv": text, "dry_run": False, "request_id": "add-resume"}
    failed = job_client.post("/api/ingestion-jobs/7/tables", json=body)
    assert failed.status_code == 202 and failed.json()["task"]["status"] == "failed"
    done = job_client.post("/api/ingestion-jobs/7/tables", json=body)
    assert done.status_code == 202 and done.json()["task"]["status"] == "succeeded"
    assert len(job_client.db.of("insert into lakefed_ingest.control (")) == 10
    assert synced == [7, 7]


def test_only_a_csv_or_a_selection_of_tables_names_its_tables_one_by_one():
    table_part = ("tables", "sql_b", "ops_01", ["t0001"], None)
    assert main._explicit_tables(None)
    assert main._explicit_tables({"database_lines": [], "parts": [table_part]})
    assert not main._explicit_tables({"database_lines": [], "parts": [("server", "sql_b", None, None, "")]})
    database_part = ("database", "sql_b", "ops_01", None, None)
    assert not main._explicit_tables({"database_lines": [], "parts": [table_part, database_part]})
    assert not main._explicit_tables({"database_lines": [{"line": 2}], "parts": [table_part]})


def test_naming_a_removed_table_in_an_add_takes_it_back_in_and_clears_its_exclusion(job_client, monkeypatch):
    _removed(monkeypatch, "sql_b", "ops_01", "gen", "t0001")
    text = HEAD + "sql_b,ops_01,gen,t0001,\"id, a\",id\n"
    r = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": text, "dry_run": False, "request_id": "add-back"})
    assert r.status_code == 200 and r.json()["applied"] is True
    assert job_client.db.of(q.clear_exclusions_sql()) == [([tr.exclusion_key("sql_b", "ops_01", "gen", "t0001")], 7)]
    assert len(job_client.db.of("insert into lakefed_ingest.control (")) == 1


def _sync_error(collection, detail):
    return {"task_collection": collection, "action": "error", "detail": detail, "job_id": None, "member_id": 1}


def _removed(monkeypatch, server, database, schema, table):
    """The job removed this table: its exclusion is on the job's options."""
    key = tr.exclusion_key(server, database, schema, table)
    removal = {key: {"removed_at": "2026-10-30T00:00:00+00:00", "removed_by": None}}
    monkeypatch.setattr(main.sch, "options_of", lambda job: {tr.EXCLUDED: removal})


def test_the_job_page_reads_its_tasks_after_ending_stale_ones(job_client):
    job_client.tasks.executed.clear()
    assert job_client.get("/api/ingestion-jobs/7/tasks").status_code == 200
    assert [p for sql, p in job_client.tasks.executed if sql == q.stale_tasks_sql()] == [
        (main.TASK_UNSYNCED, main.TASK_ROLLED_BACK, main.TASK_PARTIAL, main.TASK_STALE_S, None, 7)]


# ---- uploads and selections (their SQL runs against Postgres in large_writes_pg_test.py) -------------------------

def test_a_selection_names_one_source_and_something_to_take():
    with pytest.raises(ValueError):
        main.Selection(upload_id="u1", discovery_id="d1", whole_server=True)
    with pytest.raises(ValueError):
        main.Selection(discovery_id="d1")
    assert main.Selection(upload_id="u1").source() == ("upload", "u1", "u1")     # its database-only lines are taken
    assert main.Selection(discovery_id="d1", discovery_ids=["d2", "d1"], whole_server=True).source() == (
        "discovery", ["d1", "d2"], "d1,d2")
    assert q.selection_key("SQL_A", "Eng_1", "Gen.T1") == "sql_a\x1feng_1\x1fgen.t1"
    with pytest.raises(ValueError):
        main.NewJobRequest(name="x", csv="a", source={"upload_id": "u", "whole_server": True})


def test_a_plan_of_an_upload_is_numbered_as_the_file_is():
    plan = {"lines": [{"line": 2}, {"line": 3}]}
    assert [line["line"] for line in main._file_lines(plan, {"kind": "upload", "lines": [7, 12]})["lines"]] == [7, 12]


def test_a_selection_is_read_in_key_order_pages_and_refused_past_the_cap(monkeypatch):
    rows = [{"server": "sql_a", "src_database": f"eng_{i // 3}", "src_schema": "gen", "src_table": f"T{i}",
             "line": 10 + i, "fields": {"src_table": f"T{i}"}} for i in range(7)]
    pages = []

    def key(row):
        return (row["server"], row["src_database"].lower(), row["line"], row["src_schema"].lower(),
                row["src_table"].lower())

    def query(sql, params=()):
        assert sql == q.selected_rows_page_sql("upload")
        after, limit = tuple(params[5:10]), params[10]
        page = [dict(r, k_database=r["src_database"].lower(), k_schema=r["src_schema"].lower(),
                     k_table=r["src_table"].lower())
                for r in sorted(rows, key=key) if key(r) > after][:limit]
        pages.append(len(page))
        return page

    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main, "SELECTED_PAGE", 3)
    monkeypatch.setattr(main, "BACKGROUND_ROWS", 7)
    selection = main.Selection(upload_id="u1", whole_server=True)
    read = main._selected_rows("upload", "u1", selection)
    assert [r["fields"]["src_table"] for r in read] == [f"T{i}" for i in range(7)]
    assert pages == [3, 3, 1]

    monkeypatch.setattr(main, "BACKGROUND_ROWS", 5)
    with pytest.raises(main.HTTPException) as refused:
        main._selected_rows("upload", "u1", selection)
    assert refused.value.status_code == 422 and "grew past 5 tables" in refused.value.detail["errors"][0]


def test_create_by_reference_plans_the_lines_read_back_from_lakebase(client, monkeypatch):
    asked = []

    def query(sql, params=()):
        if sql == q.upload_sql():
            return [{"upload_id": "u1", "columns": ["src_connection", "src_database", "src_schema", "src_table", "select_list",
                                                    "primary_key"]}]
        if sql == q.selected_rows_page_sql("upload"):
            asked.append(params)
            return [{"server": "sql_a", "src_database": "eng_001", "src_schema": "gen", "src_table": f"t{i}", "line": 10 + i,
                     "fields": {"src_connection": "sql_a", "src_database": "eng_001", "src_schema": "gen",
                                "src_table": f"t{i}", "select_list": "id, a", "primary_key": "id"}} for i in range(3)]
        return client.tasks.query(sql, params)

    monkeypatch.setattr(main.db, "query", query)
    source = {"upload_id": "u1", "databases": [{"server": "SQL_A", "database": "eng_001"}],
              "tables": [{"server": "sql_a", "database": "eng_002", "tables": ["gen.t9"]}]}
    r = client.post("/api/ingestion-jobs", json={"name": "Ref", "settings": FAN_IN, "source": source})
    assert r.status_code == 200 and r.json()["summary"]["tables"] == 3 and client.db.statements == []
    assert r.json()["summary"]["invalid"] == []
    assert asked[0][:5] == ("u1", False, "", ["sql_a\x1feng_001"], ["sql_a\x1feng_002\x1fgen.t9"])
    r = client.post("/api/ingestion-jobs", json={"name": "Ref", "settings": FAN_IN, "source": source, "dry_run": False})
    assert r.json()["created"] is True
    kept = client.db.of("insert into lakefed_ingest.job_selection")
    assert len(kept) == 1        # one statement: two parts of 9 values each
    assert kept[0][:9] == (7, "database", "SQL_A", "eng_001", None, None, "upload", "u1", "op@example.com")
    assert kept[0][9:13] == (7, "tables", "sql_a", "eng_002")
