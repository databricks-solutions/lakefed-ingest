"""A job's groups spread again when its number of groups per server changes (main._regroup_plan, the endpoint's
refusals): only servers whose number changes move, few databases move, none is split, and nothing moves while a run
or a write of the job is going. The apply itself runs against Postgres (large_writes_pg_test.py)."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402


def _rows(groups, sizes, server="sql_a", job="job"):
    """Control rows of one server: database i of sizes[i] tables in groups[i] (a group number)."""
    rows, next_id = [], 1
    for i, (group, tables) in enumerate(zip(groups, sizes)):
        for t in range(tables):
            rows.append({"id": next_id, "task_collection": f"{job}_{server}_c{group:02d}", "src_connection": server,
                         "use_remote_query": True, "src_host": None, "src_port": None, "src_database": f"db{i:02d}",
                         "src_table": f"t{t}"})
            next_id += 1
    return rows


def test_more_groups_move_only_what_the_new_groups_take():
    rows = _rows([1] * 6 + [2] * 6, [10] * 12)
    plan = main._regroup_plan("job", rows, 3)
    assert plan["new"] == ["job_sql_a_c03"] and plan["emptied"] == []
    assert plan["summary"]["databases_moving"] == 4 and plan["summary"]["tables_moving"] == 40
    after = {}
    for r in rows:
        group = next((a for i, _, a, _ in plan["moves"] if i == r["id"]), r["task_collection"])
        after.setdefault(r["src_database"], set()).add(group)
    assert all(len(groups) == 1 for groups in after.values())          # a database is never split
    counts = {}
    for groups in after.values():
        counts[next(iter(groups))] = counts.get(next(iter(groups)), 0) + 1
    assert sorted(counts.values()) == [4, 4, 4]


def test_an_unchanged_number_moves_nothing():
    plan = main._regroup_plan("job", _rows([1, 2, 2], [5, 1, 1]), 2)
    assert plan["moves"] == [] and plan["summary"]["unchanged"] is True


def test_blank_takes_the_default_by_tables_and_fewer_groups_empty_some():
    plan = main._regroup_plan("job", _rows([1, 2, 3, 4], [5, 5, 5, 5]), None)       # 20 tables: one group by default
    assert plan["summary"]["servers"][0]["groups_after"] == 1 and len(plan["emptied"]) == 3
    assert {after for _, _, after, _ in plan["moves"]} == {"job_sql_a_c01"}


def test_regrouping_waits_for_a_run_or_a_write_of_the_job(monkeypatch):
    job, members = {"id": 7}, [{"task_collection": "job_sql_a_c01", "removed_at": None}]
    monkeypatch.setattr(main, "_job_active_runs", lambda job, members: [{"run_id": 1}])
    assert "a run of this job is going" in main._regroup_blocker(job, members)
    monkeypatch.setattr(main, "_job_active_runs", lambda job, members: [])
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"status": "running"}])
    assert "still going" in main._regroup_blocker(job, members)
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"status": "succeeded"}])
    assert main._regroup_blocker(job, members) is None


class _Column:
    def __init__(self, name):
        self.name = name


class _Cursor:
    """A cursor of the fake Lakebase: answers each statement from its table of answers (by the queries' own SQL) and
    records it; every collection lock is free."""

    def __init__(self, store):
        self.store, self.rows, self.description = store, [], []

    def execute(self, sql, params=()):
        self.store.statements.append(sql)
        if sql == q.collection_locks_sql():
            self.description, self.rows = [_Column("key"), _Column("taken")], [(key, True) for key in params[0]]
            return
        answer = self.store.answers.get(sql, [])
        self.description = [_Column(name) for name in (answer[0] if answer else {})]
        self.rows = [tuple(row.values()) for row in answer]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Connection:
    def __init__(self, store):
        self.store, self.autocommit = store, True

    def cursor(self):
        return _Cursor(self.store)

    def commit(self):
        self.store.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Lakebase:
    def __init__(self, answers):
        self.answers, self.statements, self.commits = answers, [], 0

    def connect(self):
        return _Connection(self)


def _regroup_client(monkeypatch, asked, stub_run_check=True, tables=10):
    """The app over a fake Lakebase holding one job of 12 databases in 2 groups, with the workspace guard on (conftest):
    only the calls stubbed here may reach Databricks, so any other one on the regroup path fails the test."""
    rows = _rows([1] * 6 + [2] * 6, [tables] * 12, job="re")
    job = {"id": 7, "name": "re", "cadence_minutes": 60, "stagger": False, "job_variant": "check"}
    members = [{"id": n, "schedule_id": 7, "task_collection": f"re_sql_a_c0{n}", "removed_at": None, "job_id": None,
                "offset_minutes": 0} for n in (1, 2)]
    store = _Lakebase({q.schedule_sql(): [job], q.schedule_members_sql(): members,
                       q.groups_control_rows_sql(): rows, q.groups_control_rows_sql(for_update=True): rows,
                       q.insert_task_sql(): [{"id": 1, "made": True}]})
    monkeypatch.setattr(main.db, "connect", store.connect)
    # A check job, as a deployed app has (no id on the Mac otherwise), so the run check reads its runs.
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "2")
    if stub_run_check:
        monkeypatch.setattr(main, "_job_active_runs", lambda job, members: asked.append("runs") or [])
    monkeypatch.setattr(main, "_sync", lambda job_id, user: asked.append("sync") or [])
    monkeypatch.setattr(main, "_refuse_job_tables", lambda job, groups, request: None)
    return TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"}), store


def test_the_regroup_preview_and_apply_reach_databricks_only_for_the_run_check_and_the_sync(monkeypatch):
    # The Postgres test missed the run check's stub; this one reaches the same calls on the Mac.
    asked = []
    client, store = _regroup_client(monkeypatch, asked)
    preview = client.post("/api/ingestion-jobs/7/groups", json={"groups_per_server": 3})
    assert preview.status_code == 200 and preview.json()["blocked"] is None, preview.text
    assert preview.json()["summary"]["new_groups"] == ["re_sql_a_c03"] and asked == ["runs"]
    assert not any(s.lstrip().lower().startswith(("update", "insert", "delete")) for s in store.statements)
    done = client.post("/api/ingestion-jobs/7/groups", json={"groups_per_server": 3, "dry_run": False,
                                                            "request_id": "regroup-mac"})
    assert done.status_code == 200, done.text
    assert done.json()["summary"]["applied"] is True and asked == ["runs", "runs", "sync"]
    assert q.move_controls_sql() in store.statements and store.commits >= 1


def test_an_unstubbed_run_check_on_the_regroup_path_fails_at_the_workspace_guard(monkeypatch):
    """_regroup_blocker asks Databricks whether a run of the job is going, so a test of the endpoint that leaves it
    unstubbed fails here, on the Mac, as the Postgres test did, and never reaches a real workspace."""
    client, _ = _regroup_client(monkeypatch, [], stub_run_check=False)
    with pytest.raises(RuntimeError, match="never reach a Databricks workspace"):
        client.post("/api/ingestion-jobs/7/groups", json={"groups_per_server": 3})


def test_a_new_tables_per_group_regroups_by_size_and_is_kept_on_the_job(monkeypatch):
    plan = main._regroup_plan("job", _rows([1] * 6 + [2] * 6, [10] * 12), main.placement.TablesPerGroup(40))
    assert plan["summary"]["servers"][0]["groups_after"] == 3      # 120 tables, 40 a group
    asked = []
    client, store = _regroup_client(monkeypatch, asked, tables=100)
    for size, moved in ((400, True), (1_000, False)):      # 1,200 tables: 3 groups at 400, the same 2 at 1,000
        store.statements.clear()
        done = client.post("/api/ingestion-jobs/7/groups", json={"tables_per_group": size, "dry_run": False,
                                                                "request_id": f"regroup-size-{size}"})
        assert done.status_code == 200, done.text
        assert (main.q.move_controls_sql() in store.statements) is moved
        assert main.q.set_schedule_option_sql() in store.statements   # the job keeps its new size either way


def test_a_job_keeps_the_tables_per_group_create_names_and_its_later_adds_use_it():
    body = main.NewJobRequest(name="n", csv="x", tables_per_group=20_000)
    assert main._create_options(body)[main.TABLES_PER_GROUP_OPTION] == 20_000
    assert main._create_options(main.NewJobRequest(name="n", csv="x")) == {
        main.js.TARGET_NAMES_OPTION: main.js.UNITY_CATALOG_NAMES}
    tables = main.JobTablesRequest(csv="x")
    job = {"options": {main.TABLES_PER_GROUP_OPTION: 20_000}}
    assert main._sizing(tables, job) == main.placement.TablesPerGroup(20_000)
    assert main._sizing(main.JobTablesRequest(csv="x", tables_per_group=500), job) == main.placement.TablesPerGroup(500)
    assert main._sizing(tables, {"options": {}}) is None
    # A request can't set it through the run's options: only Create's field and a regroup do.
    assert main.TABLES_PER_GROUP_OPTION in main.MANAGED_OPTIONS
