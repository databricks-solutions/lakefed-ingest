"""Create at once and the prepare task (main._create_preparing, _prepare_job), end to end against fakes of Lakebase,
the source and the reader: the job recorded at once and its record's states (Preparing, Needs you, Ready), a database
set aside whole when it couldn't be read or clashes (each kind), never an empty job, the resume after a restart with
nothing read or written twice, one prepare per job across instances, Stop preparing and Delete while preparing, and
the checks at the write held to the maker as they are then. The SQL itself runs against Postgres in
prepare_pg_test.py."""
import contextlib
import copy
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import by_database as bd  # noqa: E402
import main  # noqa: E402
import preparing as p  # noqa: E402
import queries as q  # noqa: E402
from api_test import FakeDB  # noqa: E402
from by_database_test import ByDatabaseCursor, ByDatabaseTasks, Store  # noqa: E402
from new_job_test import FAN_IN  # noqa: E402

NAME = "Big job"
DEDICATED = {"fan_in": False, "sink_catalog": "main", "sink_schema": "bronze_{database}", "sink_table": "{table}"}


class Source:
    """A server's databases ({database: [table names]}) as the app sees them: an upload's lines, or a discovery's
    rows, each database read (done), still reading (pending) or failed with why."""

    def __init__(self, databases, by_host=False):
        self.databases, self.by_host = databases, by_host
        self.pending, self.failed, self.dropped, self.discarded = set(), {}, set(), set()
        self.left_out = set()

    def read(self):
        return [d for d in sorted(self.databases) if d not in self.pending and d not in self.failed
                and d not in self.dropped]

    def fields(self, database, table=None):
        source = ({"src_host": "orders.internal.example", "src_port": "1433", "secret_key": f"orders-{database}"}
                  if self.by_host else {"src_connection": "sql_a"})
        return {**source, "src_database": database, **({"src_schema": "gen", "src_table": table, "select_list": "id, a",
                                                         "primary_key": "id"} if table else {})}

    def rows(self, databases=None):
        wanted = set(self.read()) if databases is None else set(databases) & set(self.read())
        return [{"server": "sql_a", "src_database": d, "src_schema": "gen", "src_table": t, "line": at + 2,
                 "fields": self.fields(d, t)} for d in sorted(wanted) for at, t in enumerate(self.databases[d])]


class PrepareCursor(ByDatabaseCursor):
    """The by-database write's cursor, with the statements Create at once and the prepare task add."""

    def __init__(self, fake, store, tasks):
        super().__init__(fake, store, tasks)

    def execute(self, sql, params=()):
        s = self.store
        if sql == q.set_prepare_sql():
            s.options[params[1]] = {**s.options.get(params[1], {}), p.OPTION: json.loads(params[0])}
            return
        if sql == q.insert_task_sql():
            task_id = len(self.tasks.rows) + 1
            self.tasks.rows[task_id] = dict(id=task_id, kind=params[0], request_id=params[1], job_id=params[2],
                                            status="running", done=0, total=params[3], result=None, error=None,
                                            created_by=params[5], progress=None)
            return
        if sql.startswith("insert into lakefed_ingest.job_selection"):
            width = len(q.JOB_SELECTION_COLUMNS)
            for at in range(0, len(params), width):
                part = dict(zip(q.JOB_SELECTION_COLUMNS, params[at:at + width]))
                s.selection.setdefault(part["job_id"], []).append(part)
            return
        if sql == q.draft_claim_sql():
            s.claimed.append(params[0])
            return
        if sql == q.update_schedule_sql(["paused"]):
            s.paused[params[1]] = params[0]
            return
        if sql == q.delete_schedule_sql():
            s.deleted[params[0]] = datetime.now(timezone.utc).isoformat()
            return
        if sql == q.finish_prepare_sql():
            _finish(self.tasks, *params)
            return
        if sql == q.log_change_sql():
            s.log.append(params)
        super().execute(sql, params)
        if sql == q.insert_schedule_sql():
            sid = self._rows[0][0]
            s.options[sid], s.paused[sid] = json.loads(params[8]), params[6]


def _finish(tasks, status, error, job_id, owner, _owner):
    for row in tasks.rows.values():
        if row["kind"] == q.PREPARE_KIND and row["job_id"] == job_id and row["status"] == "running" \
                and (owner is None or (row.get("progress") or {}).get("owner") == owner):
            row.update(status=status, error=error)


@pytest.fixture
def prepared(monkeypatch):
    return _client(monkeypatch, Source({f"eng_{i:03d}": [f"t{t}" for t in range(5)] for i in range(30)}))


def _client(monkeypatch, source, kind="upload"):
    store, tasks, fake = Store(), ByDatabaseTasks(), FakeDB()
    store.options, store.paused, store.deleted, store.selection, store.claimed = {}, {}, {}, {}, []
    store.owners, store.log, store.stale, store.cancelled, store.not_managers = [], [], set(), [], set()
    store.refusals = {"run": None, "change": None, "delete": None}
    store.source, store.kind = source, kind

    def schedule(sid):
        if sid not in store.schedules:
            return []
        return [dict(id=sid, name=NAME, cadence_minutes=60, timezone="UTC", stagger=True, job_variant="default",
                     parameters={}, notify_emails=None, options=json.dumps(store.options.get(sid) or {}),
                     paused=store.paused.get(sid, True), deleted_at=store.deleted.get(sid), created_by="op")]

    def keys_after(after, filter_, limit):
        keys = sorted((("sql_a", d.lower(), d) for d in source.read() if filter_.lower() in d.lower()))
        return [{"server": s, "src_database": d, "tables": len(source.databases[d])} for s, _, d in keys
                if (s, d.lower(), d) > after][:limit]

    def failed_rows():
        return [{"server": "sql_a", "src_database": d, "error": why, "whole_server": why.startswith("closed:")}
                for d, why in sorted(source.failed.items()) if d not in source.dropped and d not in source.left_out]

    def query(sql, params=()):
        either = lambda name, *args: sql in (getattr(q, name)("upload", *args), getattr(q, name)("discovery", *args))
        if sql == q.schedule_sql():
            return schedule(params[0])
        if sql == q.schedule_members_sql():
            return [{"task_collection": g, "removed_at": None} for sid, g in store.members if sid == params[0][0]]
        if either("selected_count_sql"):
            return [{"n": len(source.rows())}]
        if either("selected_servers_sql"):
            rows = source.rows()
            return [{"server": "sql_a", "databases": len(source.read()), "tables": len(rows)}] if rows else []
        if either("selected_databases_sql"):
            return [{"server": "sql_a", "src_database": d.lower(), "tables": len(source.databases[d])}
                    for d in source.read()]
        if either("source_databases_after_sql"):
            return keys_after(tuple(params[2:5]), params[1], params[5])
        if either("database_rows_chunk_sql"):
            return source.rows([key.split("\x1f")[1] for key in params[1]])
        if sql == q.database_rows_exact_sql():
            return source.rows(params[1])
        if sql == q.upload_sql():
            return [{"upload_id": "u1", "columns": ["src_connection", "src_host", "src_port", "secret_key",
                                                    "src_database", "src_schema", "src_table", "select_list",
                                                    "primary_key"]}]
        if sql == q.upload_database_sources_sql():
            return [{"server": "sql_a", "src_database": d, "fields": source.fields(d)} for d in sorted(source.databases)]
        if sql == q.selection_discoveries_sql():
            return [{"server": "sql_a", "src_type": "sqlserver", "src_connection": "sql_a", "src_database": d,
                     "listing": {"dropped": True} if d in source.dropped else {}} for d in sorted(source.databases)]
        if sql == q.draft_reads_sql():
            live = [d for d in source.databases if d not in source.dropped]
            return [{"discovery_id": "d1", "src_connection": "sql_a", "databases": len(live),
                     "pending": len([d for d in source.pending if d not in source.discarded]),
                     "done": len(source.read()), "failed": len(source.failed), "discarded": 0,
                     "tables": len(source.rows())}]
        if sql == q.draft_failed_sql():
            return [dict(r) for r in failed_rows()]
        if sql == q.job_reads_sql():
            return [{"databases": len(source.databases), "pending": len(source.pending), "done": len(source.read()),
                     "failed": len(source.failed), "tables": len(source.rows()), "recent": 0, "elapsed_s": 1.0}]
        if sql == q.job_failed_reads_sql():
            rows = failed_rows()
            return [dict(r, total=len(rows)) for r in rows][params[3]:params[3] + params[2]]
        if sql == q.database_owners_sql():
            return list(store.owners)
        if sql == q.job_selection_sql():
            return [dict(part, id=at) for at, part in enumerate(store.selection.get(params[0], []))]
        if sql == q.job_database_groups_sql():
            return [{"server": "sql_a", "src_database": d, "task_collection": g}
                    for d, g in sorted({(row["src_database"].lower(), row["task_collection"]) for row in store.controls
                                        if row["task_collection"] in params[0]})]
        if sql == q.collections_sql():
            return [{"task_collection": g} for g in sorted({row["task_collection"] for row in store.controls})]
        if sql == q.groups_row_count_sql():
            return [{"n": sum(1 for row in store.controls if row["task_collection"] in params[0])}]
        if sql == q.claim_prepare_sql():
            return _claim(tasks, store, *params)
        if sql == q.prepare_beat_sql():
            row = tasks.rows.get(params[0])
            return [{"id": params[0]}] if _holds(row, params[1]) else []
        if sql == q.preparing_jobs_sql():
            return [{"id": sid} for sid in store.schedules if not store.deleted.get(sid)
                    and (store.options.get(sid, {}).get(p.OPTION) or {}).get("state") == p.PREPARING
                    and not (store.options[sid][p.OPTION]).get("stopped")]
        if sql == q.prepare_stop_reads_sql():
            going = [{"statement_id": f"st-{d}"} for d in sorted(source.pending - source.discarded)]
            source.discarded |= source.pending
            return going
        if sql == q.prepare_resume_reads_sql():
            resumed = len(source.discarded)
            source.discarded = set()
            return [{"resumed": resumed}]
        if sql in (q.prepare_read_again_sql(), q.prepare_leave_out_reads_sql()):
            _, _, every, _, names = params
            hit = [r for r in failed_rows() if every or r["src_database"].lower() in names]
            for r in hit:
                if sql == q.prepare_read_again_sql():
                    source.pending.add(source.failed.pop(r["src_database"]) and r["src_database"])
                else:
                    source.left_out.add(r["src_database"])
            return hit
        return tasks.query(sql, params)

    def fetch(cur, sql, params=()):
        if sql == q.prepare_fence_sql():
            return [{"id": params[0]}] if _holds(tasks.rows.get(params[0]), params[1]) else []
        return query(sql, params)

    def execute(sql, params=()):
        if sql == q.finish_prepare_sql():
            _finish(tasks, *params)
        elif sql != q.draft_touch_sql():
            tasks.execute(sql, params)

    @contextlib.contextmanager
    def transaction():
        store.transactions += 1
        saved = copy.deepcopy((store.controls, store.sinks, store.members, store.schedules, tasks.rows, store.options,
                               store.selection, store.paused, store.deleted))
        try:
            yield PrepareCursor(fake, store, tasks)
        except BaseException:
            (store.controls, store.sinks, store.members, store.schedules, tasks.rows, store.options, store.selection,
             store.paused, store.deleted) = saved
            raise

    def advance(store_, warehouse_, kind_, discovery_id, connection, first=()):
        """One step of the reads, as warehouse_reads.advance takes it with the reader off: every pending database is
        read, or fails when the test says it does."""
        for database in sorted(source.pending - source.discarded):
            source.pending.discard(database)
            if database in source.will_fail:
                source.failed[database] = source.will_fail[database]
        return {}

    source.will_fail = {}
    monkeypatch.setattr(main, "PREPARE_CREATES", True)
    monkeypatch.setattr(main, "PREPARE_POLL_S", 0)
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "fetch", fetch)
    monkeypatch.setattr(main.db, "execute", execute)
    monkeypatch.setattr(main.db, "transaction", transaction)
    monkeypatch.setattr(main.warehouse_reads, "advance", advance)
    monkeypatch.setattr(main.warehouse, "cancel", store.cancelled.append)
    monkeypatch.setattr(main.jobs, "schedule_variants", lambda: {"default": "1", "check": "2"})
    monkeypatch.setattr(main, "_start_background", tasks.started.append)
    monkeypatch.setattr(main, "BACKGROUND_ROWS", 50)
    monkeypatch.setattr(main, "TASK_BEAT_S", 3600)
    monkeypatch.setattr(main, "_permission_refusals", lambda job, user: dict(store.refusals))
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager" if user in store.not_managers
                        else None)
    monkeypatch.setattr(main, "_refuse_job", lambda job, request, action: None if not store.refusals.get(action)
                        else main._refuse(store.refusals[action]))
    monkeypatch.setattr(main, "_delete_blocker", lambda job, active, groups: None)
    monkeypatch.setattr(main, "_delete_plan", lambda sid: {"groups": [], "tables": 0, "targets": 0, "kept": 0,
                                                           "kept_names": []})
    synced = []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or [])
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.store, c.tasks, c.synced, c.db, c.source = store, tasks, synced, fake, source
    return c


def _holds(row, owner) -> bool:
    return bool(row) and row["status"] == "running" and (row.get("progress") or {}).get("owner") == owner


def _claim(tasks, store, owner, job_id, stale_s, backoff_s):
    """claim_prepare_sql: the job's prepare task, when no thread has held it, it ended (a while ago, given a backoff),
    or its heartbeat is stale (store.stale)."""
    rows = [row for row in tasks.rows.values() if row["kind"] == q.PREPARE_KIND and row["job_id"] == job_id]
    if not rows:
        return []
    row = max(rows, key=lambda r: r["id"])
    free = (row["status"] == "running" and (row.get("progress") is None or row["id"] in store.stale)) or (
        row["status"] != "running" and (not backoff_s or row["id"] in store.stale))
    if not free:
        return []
    store.stale.discard(row["id"])
    row.update(status="running", error=None, progress={"owner": owner})
    return [{"id": row["id"]}]


def _create(client, settings=None, selection=None, paused=True, request_id="big-1"):
    return client.post("/api/ingestion-jobs", json={
        "name": NAME, "settings": settings or FAN_IN, "dry_run": False, "request_id": request_id,
        "run": {"cadence_minutes": 60, "paused": paused},
        "source": selection or ({"upload_id": "u1", "whole_server": True} if client.store.kind == "upload"
                                else {"discovery_ids": ["d1"], "whole_server": True})})


def _create_while_reading(client, **kwargs):
    """A Create while every read is still going: its own look at the reads moves none of them on."""
    advance = main.warehouse_reads.advance
    main.warehouse_reads.advance = lambda *args, **kw: {}
    try:
        return _create(client, **kwargs)
    finally:
        main.warehouse_reads.advance = advance


def _record(client, sid=7) -> dict:
    return client.store.options[sid][p.OPTION]


def _tables(client) -> int:
    return len(client.store.controls)


# ---- Create at once ------------------------------------------------------------------------------------------------

def test_create_records_the_job_at_once_and_answers_with_its_id(prepared):
    r = _create(prepared)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == 7 and body["created"] is True and "task" not in body
    assert body["prepare"]["state"] == p.PREPARING and body["prepare"]["stage"] == p.READING
    record = _record(prepared)
    assert record["maker"] == "op@example.com" and record["turn_on"] is False
    assert prepared.store.paused[7] is True and _tables(prepared) == 0 and prepared.store.members == set()
    assert [part["scope"] for part in prepared.store.selection[7]] == ["server"]
    [task] = prepared.tasks.rows.values()
    assert task["kind"] == q.PREPARE_KIND and task["job_id"] == 7 and task["request_id"] == "big-1"
    assert len(prepared.tasks.started) == 1                  # its thread, started after the commit


def test_the_job_goes_through_writing_and_syncing_to_ready_and_turns_on_when_asked(prepared):
    sid = _create(prepared, paused=False).json()["id"]
    assert prepared.store.paused[sid] is True and _record(prepared)["turn_on"] is True
    prepared.tasks.run()
    record = _record(prepared)
    assert record["state"] == p.READY and record["ready_at"] and record["writing_started"] is True
    assert record["progress"]["tables_written"] == 150 and record["progress"]["percent"] == 100
    assert _tables(prepared) == 150 and prepared.store.paused[sid] is False and prepared.synced == [sid, sid]
    assert prepared.tasks.rows[1]["status"] == "succeeded"
    assert {g for _, g in prepared.store.members} == {row["task_collection"] for row in prepared.store.controls}
    assert all(row["created_by"] == "op@example.com" for row in [prepared.tasks.rows[1]])


def test_a_database_the_prepare_write_splits_over_groups_is_warned_about_on_the_job_page(prepared, monkeypatch):
    """The prepare path has no page waiting for its answer, so the warning of the write that makes the split is kept
    on the job's record, which the job page reads while the job prepares (review of PR 41)."""
    def split_rows(cur, rows):
        written = {row["src_database"] for row in prepared.store.controls}
        return [(q.database_key(row), row["src_database"], ["g1", "g2"]) for row in rows
                if row["src_database"] in written and row["src_database"] == "eng_004"]

    monkeypatch.setattr(main, "_split_database_rows", split_rows)
    _create(prepared)
    prepared.tasks.run()
    [warning] = p.view({p.OPTION: _record(prepared)})["warnings"]
    assert warning["kind"] == "split_database" and warning["databases"] == [{"database": "eng_004", "groups": ["g1", "g2"]}]


def test_the_same_request_id_sent_again_answers_the_job_it_recorded(prepared):
    first = _create(prepared).json()
    again = _create(prepared)
    assert again.status_code == 200 and again.json()["id"] == first["id"] and prepared.store.schedules == [7]
    assert len(prepared.tasks.rows) == 1


def test_create_refuses_by_name_what_it_can_check_before_any_table_is_read(prepared):
    prepared.store.owners = [{"database": "eng_004", "job": "Sales nightly", "deleted": False}]
    r = _create(prepared)
    assert r.status_code == 409 and "eng_004 is in the ingestion job Sales nightly already" in r.text
    prepared.store.owners = [{"database": "eng_004", "job": "Old job", "deleted": True}]
    assert "deleted ingestion job Old job" in _create(prepared).text
    prepared.store.owners = []
    bad = _create(prepared, settings={**FAN_IN, "sink_table": "{nope}"})
    assert bad.status_code == 422
    assert prepared.store.schedules == [] and prepared.tasks.rows == {}


def test_create_refuses_a_job_whose_group_names_another_job_uses(prepared):
    prepared.store.controls.append({"task_collection": "big_job_sql_a_c03", "src_database": "x", "sink_key": ()})
    r = _create(prepared)
    assert r.status_code == 422 and "big_job_sql_a_c03" in r.text and prepared.store.schedules == []


def test_create_with_its_schedule_on_needs_the_right_to_run_what_it_starts(prepared):
    prepared.store.refusals["run"] = "you can't run the ingestion job"
    assert _create(prepared, paused=False).status_code == 403 and prepared.store.schedules == []
    assert _create(prepared, paused=True).status_code == 200


def test_a_small_choice_all_read_is_still_written_in_the_request_and_ready(prepared, monkeypatch):
    monkeypatch.setattr(main, "BACKGROUND_ROWS", 5000)
    calls = []
    monkeypatch.setattr(main, "_create_now", lambda body, request, inline=None: calls.append(body) or {"created": True})
    assert _create(prepared).json() == {"created": True} and len(calls) == 1 and prepared.store.schedules == []


# ---- reading, then set aside ---------------------------------------------------------------------------------------

@pytest.fixture
def reading(monkeypatch):
    client = _client(monkeypatch, Source({f"eng_{i:03d}": [f"t{t}" for t in range(3)] for i in range(10)}),
                     kind="discovery")
    client.source.pending = set(client.source.databases)
    return client


def test_a_create_whose_tables_are_still_read_answers_at_once_and_the_task_waits_for_the_reads(reading):
    r = _create_while_reading(reading)
    assert r.status_code == 200 and r.json()["prepare"]["stage"] == p.READING and _tables(reading) == 0
    assert reading.store.claimed == [7]                       # its drafts are the job's, in its own transaction
    reading.tasks.run()
    assert _record(reading)["state"] == p.READY and _tables(reading) == 30 and reading.source.pending == set()


def test_a_database_that_couldnt_be_read_is_set_aside_and_the_rest_are_written(reading):
    reading.source.will_fail = {"eng_003": "Login failed for user 'ingest'"}
    _create(reading)
    reading.tasks.run()
    record = _record(reading)
    assert record["state"] == p.NEEDS_YOU and p.counts_of(record)[p.UNREADABLE] == 1
    [entry] = record["set_aside"].values()
    assert (entry["database"], entry["kind"], entry["why"]) == ("eng_003", p.UNREADABLE, "Login failed for user 'ingest'")
    assert _tables(reading) == 27 and not any(row["src_database"] == "eng_003" for row in reading.store.controls)
    assert reading.store.paused[7] is True and reading.synced == []          # no Ready until each is resolved
    assert reading.tasks.rows[1]["status"] == "succeeded"


def test_read_them_again_reads_the_database_and_the_job_goes_ready(reading):
    reading.source.will_fail = {"eng_003": "Login failed"}
    _create(reading)
    reading.tasks.run()
    reading.source.will_fail = {}
    again = reading.post("/api/ingestion-jobs/7/prepare/read-again", json={})
    assert again.json()["marked"] == 1 and again.json()["state"] == p.PREPARING
    reading.tasks.run()
    assert _record(reading)["state"] == p.READY and _tables(reading) == 30


def test_leave_them_out_takes_the_database_out_of_the_choice_and_the_job_goes_ready(reading):
    reading.source.will_fail = {"eng_003": "Login failed"}
    _create(reading)
    reading.tasks.run()
    left = reading.post("/api/ingestion-jobs/7/prepare/leave-out", json={})
    assert left.json()["left_out"] == 1
    reading.tasks.run()
    record = _record(reading)
    assert record["state"] == p.READY and _tables(reading) == 27
    assert p.left_out_keys({p.OPTION: record}) == {p.database_key("sql_a", "eng_003")}


def test_a_database_a_whole_server_read_couldnt_open_is_left_out_and_listed_not_waited_for(reading):
    reading.source.will_fail = {"eng_005": "closed: the login can't open it"}
    main_no_access = main.warehouse_reads.no_access
    main.warehouse_reads.no_access = lambda error: str(error or "").startswith("closed:")
    try:
        _create(reading)
        reading.tasks.run()
    finally:
        main.warehouse_reads.no_access = main_no_access
    record = _record(reading)
    assert record["state"] == p.READY and p.counts_of(record)[p.CLOSED] == 1 and _tables(reading) == 27


def test_the_full_list_of_what_couldnt_be_read_is_paged_from_the_rows(reading):
    reading.source.will_fail = {f"eng_00{i}": "Login failed" for i in range(4)}
    _create(reading)
    reading.tasks.run()
    page = reading.get("/api/ingestion-jobs/7/prepare/set-aside", params={"limit": 3}).json()
    assert page["total"] == 4 and [e["database"] for e in page["databases"]] == ["eng_000", "eng_001", "eng_002"]
    rest = reading.get("/api/ingestion-jobs/7/prepare/set-aside", params={"limit": 3, "offset": 3}).json()
    assert [e["database"] for e in rest["databases"]] == ["eng_003"] and rest["databases"][0]["kind"] == p.UNREADABLE


def test_a_dropped_database_never_counts_in_the_job(reading):
    reading.source.dropped = {"eng_009"}
    reading.source.pending.discard("eng_009")
    _create_while_reading(reading, selection={"discovery_ids": ["d1"], "databases": [
        {"server": "sql_a", "database": f"eng_{i:03d}"} for i in range(9)]})
    reading.tasks.run()
    assert _record(reading)["state"] == p.READY and _tables(reading) == 27
    assert not any(row["src_database"] == "eng_009" for row in reading.store.controls)


# ---- clashes: each kind sets its database aside whole ---------------------------------------------------------------

def test_a_target_another_table_writes_sets_its_database_aside_and_the_rest_are_written(prepared, monkeypatch):
    real = main._insert_controls

    def taken(cur, rows, *args, **kwargs):
        if any(row["src_database"] == "eng_012" for row in rows):
            raise main.HTTPException(409, "another table already writes main.bronze.t1 for database eng_012")
        return real(cur, rows, *args, **kwargs)

    monkeypatch.setattr(main, "_insert_controls", taken)
    _create(prepared)
    prepared.tasks.run()
    record = _record(prepared)
    assert record["state"] == p.NEEDS_YOU and p.counts_of(record)[p.CLASH] == 1
    [entry] = record["set_aside"].values()
    assert entry["database"] == "eng_012" and "another table already writes" in entry["why"]
    assert _tables(prepared) == 145 and not any(row["src_database"] == "eng_012" for row in prepared.store.controls)


def test_two_of_the_jobs_tables_writing_one_target_set_the_second_database_aside(prepared):
    _create(prepared, settings=DEDICATED | {"sink_schema": "bronze"})        # every database writes main.bronze.t0
    prepared.tasks.run()
    record = _record(prepared)
    assert record["state"] == p.NEEDS_YOU and p.counts_of(record)[p.CLASH] == 29
    assert _tables(prepared) == 5 and all("already writes" in e["why"] for e in record["set_aside"].values())


def test_a_sink_both_dedicated_and_shared_sets_its_database_aside(prepared):
    prepared.db.mixed = {"main.bronze.t2"}
    _create(prepared, settings={**FAN_IN, "sink_schema": "bronze"})
    prepared.tasks.run()
    record = _record(prepared)
    assert record["state"] == p.NEEDS_YOU and p.counts_of(record)[p.CLASH] == 30
    assert all("dedicated row and by other rows" in e["why"] for e in record["set_aside"].values())
    assert _tables(prepared) == 0 and prepared.store.members == set()


def test_change_the_target_names_plans_the_clashing_databases_again(prepared):
    _create(prepared, settings=DEDICATED | {"sink_schema": "bronze"})
    prepared.tasks.run()
    changed = prepared.post("/api/ingestion-jobs/7/prepare/targets",
                            json={"sink_schema": "bronze_{database}", "dry_run": False})
    assert changed.json()["replan"] == 29 and changed.json()["state"] == p.PREPARING
    prepared.tasks.run()
    assert _record(prepared)["state"] == p.READY and _tables(prepared) == 150


# ---- never an empty job ---------------------------------------------------------------------------------------------

def test_a_job_whose_every_database_is_refused_is_never_ready_empty(prepared):
    prepared.db.mixed = {f"main.bronze.t{t}" for t in range(5)}
    _create(prepared, settings={**FAN_IN, "sink_schema": "bronze"})
    prepared.tasks.run()
    record = _record(prepared)
    assert record["state"] == p.NEEDS_YOU and _tables(prepared) == 0 and prepared.synced == []
    prepared.post("/api/ingestion-jobs/7/prepare/leave-out", json={})
    prepared.tasks.run()
    record = _record(prepared)
    assert record["state"] != p.READY and record["stopped"]["why"] == main.NOTHING_LEFT
    assert prepared.store.paused[7] is True and prepared.synced == []


# ---- resume, one prepare per job -------------------------------------------------------------------------------------

class Restart(BaseException):
    """The app going away in the middle of a write: nothing in the thread catches it."""


def test_a_restart_mid_write_is_resumed_from_its_stale_heartbeat_and_nothing_is_written_twice(prepared, monkeypatch):
    monkeypatch.setattr(bd, "TABLES_PER_TRANSACTION", 50)     # ten databases to a transaction
    real = main._write_chunk
    calls = {"n": 0}

    def dies_on_the_third(cur, plans, job, state):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Restart()
        return real(cur, plans, job, state)

    monkeypatch.setattr(main, "_write_chunk", dies_on_the_third)
    _create(prepared)
    with pytest.raises(Restart):
        prepared.tasks.run()
    assert _tables(prepared) == 100 and prepared.tasks.rows[1]["status"] == "running"
    assert main._resume_preparing() == []                     # its heartbeat is still fresh: no one takes it
    prepared.store.stale.add(1)
    monkeypatch.setattr(main, "_write_chunk", real)
    statements = prepared.store.control_statements
    assert main._resume_preparing() == [7]
    prepared.tasks.run()
    assert _record(prepared)["state"] == p.READY and _tables(prepared) == 150
    assert prepared.store.control_statements - statements == 1 and prepared.store.schedules == [7]


def test_a_failed_pass_is_tried_again_only_after_the_backoff(prepared, monkeypatch):
    monkeypatch.setattr(main, "_sync", lambda sid, user: (_ for _ in ()).throw(RuntimeError("the Jobs API is down")))
    _create(prepared)
    prepared.tasks.run()
    assert prepared.tasks.rows[1]["status"] == "failed" and "the Jobs API is down" in prepared.tasks.rows[1]["error"]
    assert _record(prepared)["stage"] == p.SYNCING and _tables(prepared) == 150
    assert main._resume_preparing() == []
    prepared.store.stale.add(1)                               # ended long enough ago
    monkeypatch.setattr(main, "_sync", lambda sid, user: prepared.synced.append(sid) or [])
    assert main._resume_preparing() == [7]
    prepared.tasks.run()
    assert _record(prepared)["state"] == p.READY and _tables(prepared) == 150


def test_two_instances_never_prepare_one_job_at_once(prepared):
    _create(prepared)
    assert len(prepared.tasks.started) == 1
    assert main._start_prepare(7) is None and len(prepared.tasks.started) == 1     # the other instance's ask
    owner = prepared.tasks.rows[1]["progress"]["owner"]
    prepared.tasks.rows[1]["progress"] = {"owner": "another instance"}            # it took the task over
    prepared.tasks.run()
    assert _tables(prepared) == 0 and _record(prepared)["state"] == p.PREPARING and owner != "another instance"


# ---- Stop preparing, Prepare again, Delete --------------------------------------------------------------------------

def test_stop_preparing_cancels_the_reads_and_prepare_again_goes_on_from_there(reading):
    _create_while_reading(reading)
    stopped = reading.post("/api/ingestion-jobs/7/prepare/stop")
    assert stopped.status_code == 200 and stopped.json()["cancelled"] == 10 and stopped.json()["stopped"] is True
    assert sorted(reading.store.cancelled)[:2] == ["st-eng_000", "st-eng_001"]
    assert reading.tasks.rows[1]["status"] == "failed" and reading.tasks.rows[1]["error"] == "stopped by op@example.com"
    reading.tasks.run()                                        # the thread sees it stopped and writes nothing
    assert _tables(reading) == 0 and _record(reading)["stopped"]["by"] == "op@example.com"
    assert main._resume_preparing() == []
    again = reading.post("/api/ingestion-jobs/7/prepare/again")
    assert again.json()["started"] is True and "stopped" not in _record(reading)
    reading.tasks.run()
    assert _record(reading)["state"] == p.READY and _tables(reading) == 30


def test_delete_while_preparing_stops_the_prepare_first_and_the_heartbeat_never_blocks_it(reading):
    _create_while_reading(reading)
    deleted = reading.post("/api/schedules/7/delete")
    assert deleted.status_code == 200, deleted.text
    assert reading.store.deleted.get(7) and len(reading.store.cancelled) == 10
    assert _record(reading)["stopped"]["why"] == "the job was deleted by op@example.com"
    reading.tasks.run()
    assert _tables(reading) == 0


def test_stop_and_prepare_again_follow_the_change_permission(reading):
    reading.store.refusals["change"] = "you need Can Manage on the job"
    _create_while_reading(reading)
    assert reading.post("/api/ingestion-jobs/7/prepare/stop").status_code == 403
    assert reading.post("/api/ingestion-jobs/7/prepare/again").status_code == 403
    assert "stopped" not in _record(reading)


# ---- the checks at the write, held to the maker as they are then ----------------------------------------------------

def test_a_maker_who_lost_the_manager_right_since_create_stops_the_write_saying_why(monkeypatch):
    client = _client(monkeypatch, Source({f"eng_{i:03d}": ["t0", "t1"] for i in range(30)}, by_host=True))
    assert _create(client).status_code == 200                 # an app manager at Create
    client.store.not_managers.add("op@example.com")           # no longer one at the write
    client.tasks.run()
    record = _record(client)
    assert _tables(client) == 0 and "only an app manager may set one up" in record["stopped"]["why"]
    assert record["state"] == p.PREPARING and client.tasks.rows[1]["status"] == "failed"
    client.store.not_managers.clear()
    client.post("/api/ingestion-jobs/7/prepare/again")
    client.tasks.run()
    assert _record(client)["state"] == p.READY and _tables(client) == 60


def test_each_transaction_runs_the_checks_again_with_the_stored_maker(prepared, monkeypatch):
    monkeypatch.setattr(bd, "TABLES_PER_TRANSACTION", 50)
    seen = []
    real = main._prepare_checks
    monkeypatch.setattr(main, "_prepare_checks", lambda sid, rows=None, cur=None: seen.append(
        (len(rows or []), cur is not None)) or real(sid, rows, cur))
    _create(prepared)
    prepared.tasks.run()
    assert seen == [(0, False), (50, True), (50, True), (50, True)]


# ---- a file with database-only lines ---------------------------------------------------------------------------------

@pytest.fixture
def database_lines(prepared, monkeypatch):
    """An upload of two databases' table lines and two database-only lines (hr_1, hr_2), which one discovery run reads
    by name: hr_1 has a table, hr_2 can't be opened. The run is started once, and kept under its key as the app does."""
    source = prepared.source
    rows = source.rows(["eng_000", "eng_001"])
    columns = list(rows[0]["fields"])
    selected = {"text": main.nj.csv_of(columns, [r["fields"] for r in rows]), "rows": rows, "columns": columns,
                "lines": [r["line"] for r in rows], "parts": [("server", "sql_a", None, None, None)],
                "database_lines": [{"line": 20, "database": "hr_1", "connection": "sql_a"},
                                   {"line": 21, "database": "hr_2", "connection": "sql_a"}],
                "kind": "upload", "source_id": "u1"}
    prepared.runs = []

    def resolve(chosen, wait, request=None):
        key = ("sql_a", ("hr_1", "hr_2"))
        read = main._database_read(key)
        if read is None:
            prepared.runs.append(request)
            with main._database_reads_lock:
                main._database_reads[key] = {"run_id": 55, "discovery_id": "dd", "ended": None,
                                              "started": main.time.monotonic()}
            return {"discovering": True, "databases": 2, "rows": [], "unreadable": []}
        return {"discovering": False, "databases": 2, "unreadable": [{"line": 21, "database": "hr_2",
                                                                      "error": "Login failed for user 'ingest'"}],
                "rows": [{"line": 20, "server": "sql_a", "src_database": "hr_1", "src_schema": "gen",
                          "src_table": "t0", "fields": source.fields("hr_1", "t0")}]}

    main._database_reads.clear()
    monkeypatch.setattr(main, "_has_database_lines", lambda selection: True)
    monkeypatch.setattr(main, "_selected", lambda selection: copy.deepcopy(selected))
    monkeypatch.setattr(main, "_resolve_databases", resolve)
    monkeypatch.setattr(main.jobs, "DISCOVER_JOB_ID", "9")
    prepared.discovery_right = []
    monkeypatch.setattr(main, "_refuse_run_of", lambda job_id, request: prepared.discovery_right.append(job_id))
    yield prepared
    main._database_reads.clear()


def test_a_file_with_database_only_lines_is_recorded_at_once_and_its_lines_read_by_the_task(database_lines):
    r = _create(database_lines)
    assert r.status_code == 200 and r.json()["prepare"]["state"] == p.PREPARING and database_lines.runs == []
    assert database_lines.discovery_right == ["9"]           # the right to run discovery, checked at Create
    database_lines.tasks.run()
    record = _record(database_lines)
    assert database_lines.runs == [None] and record["database_read"] == {"run_id": 55, "discovery_id": "dd"}
    assert record["state"] == p.NEEDS_YOU and p.counts_of(record) == {p.UNREADABLE: 1, p.CLASH: 0, p.CLOSED: 0}
    [entry] = record["set_aside"].values()
    assert (entry["database"], entry["why"]) == ("hr_2", "Login failed for user 'ingest'")
    assert sorted({row["src_database"] for row in database_lines.store.controls}) == ["eng_000", "eng_001", "hr_1"]
    assert _tables(database_lines) == 11


def test_a_resumed_file_waits_for_the_same_discovery_run_and_writes_nothing_twice(database_lines):
    _create(database_lines)
    database_lines.tasks.run()
    main._database_reads.clear()                              # the app restarted: its memory of the run is gone
    database_lines.post("/api/ingestion-jobs/7/prepare/leave-out", json={})
    database_lines.tasks.run()
    assert database_lines.runs == [None] and _record(database_lines)["state"] == p.READY and _tables(database_lines) == 11


def test_a_file_with_database_only_lines_needs_the_right_to_run_discovery_at_create(database_lines, monkeypatch):
    monkeypatch.setattr(main, "_refuse_run_of", lambda job_id, request: main._refuse("you can't run discovery"))
    assert _create(database_lines).status_code == 403 and database_lines.store.schedules == []
