"""A work-list run's locks (groups B): its collection's lock shared plus one exclusive lock per database it was given,
the databases another run holds skipped, and the abandoned-run close only when no other run holds the collection.
The locks themselves run against Postgres in state_store_pg_test.py."""
import json
from pathlib import Path

import planner
import state_store as ss

NOTEBOOK = Path(__file__).resolve().parents[1] / "src" / "lakefed_ingest" / "ingest.ipynb"


class _Cursor:
    """Answers the lock statements: ``held`` are the keys another session holds."""

    def __init__(self, held=(), shared_by_others=False):
        self.held, self.shared_by_others, self.calls, self.rows = set(held), shared_by_others, [], []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql == ss.try_lock_databases_sql():
            self.rows = [(key, key not in self.held) for key in params[0]]
        elif sql == ss.try_lock_collection_sql():
            self.rows = [(not self.shared_by_others,)]
        else:
            self.rows = [(True,)]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _store(cursor):
    store = ss.StateStore.__new__(ss.StateStore)
    store._cursor = lambda: cursor
    return store


def test_a_shared_hold_uses_the_same_key_as_the_exclusive_one_so_each_refuses_the_other():
    assert "pg_try_advisory_lock_shared(hashtextextended(%s, 0))" in ss.try_lock_collection_shared_sql()
    assert "pg_try_advisory_lock(hashtextextended(%s, 0))" in ss.try_lock_collection_sql()
    cursor = _Cursor()
    assert _store(cursor).try_lock_collection_shared("g1") is True
    assert cursor.calls == [(ss.try_lock_collection_shared_sql(), (ss.collection_lock_key("g1"),))]


def test_database_keys_are_per_collection_source_and_database():
    keys = planner.work_database_keys("g1", {("srv", "db1"), ("srv", "db2")})
    assert keys == {"lakefed_ingest.database:g1|srv|db1": ("srv", "db1"),
                    "lakefed_ingest.database:g1|srv|db2": ("srv", "db2")}
    assert ss.database_lock_key("g1", "srv", "db1") != ss.collection_lock_key("g1|srv|db1")


def test_a_database_another_run_holds_is_skipped_and_the_rest_kept():
    work = {("srv", "db1"), ("srv", "db2"), ("srv", "db3")}
    keys = planner.work_database_keys("g1", work)
    cursor = _Cursor(held={ss.database_lock_key("g1", "srv", "db2")})
    held = _store(cursor).try_lock_databases(keys)
    assert len(cursor.calls) == 1                                # one statement for the whole list
    kept, taken = planner.work_after_locks(work, keys, held)
    assert kept == {("srv", "db1"), ("srv", "db3")} and taken == [("srv", "db2")]
    assert planner.work_after_locks(work, keys, set()) == (set(), sorted(work))


def test_a_run_is_alone_on_its_collection_only_when_no_other_session_holds_it():
    alone = _Cursor()
    assert _store(alone).alone_on_collection("g1") is True
    assert [sql for sql, _ in alone.calls] == [ss.try_lock_collection_sql(), ss.unlock_collection_sql()]
    shared = _Cursor(shared_by_others=True)
    assert _store(shared).alone_on_collection("g1") is False
    assert [sql for sql, _ in shared.calls] == [ss.try_lock_collection_sql()]       # nothing to let go of


def test_the_rollup_lock_takes_each_group_once_in_order():
    sql = ss.rollup_group_lock_sql()
    assert sql.count("%s") == 2 and "pg_advisory_xact_lock" in sql and "order by g" in sql


def _run_source():
    return "".join(json.loads(NOTEBOOK.read_text())["cells"][2]["source"])


def test_the_run_holds_its_collection_shared_with_a_work_list_and_alone_without():
    source = _run_source()
    assert ("locked = (lock_store.try_lock_collection_shared(task_collection) if work_id\n"
            "          else lock_store.try_lock_collection(task_collection))") in source
    assert "lock_store.try_lock_databases(database_locks)" in source
    assert "work_id and not lock_store.alone_on_collection(task_collection)" in source
    # The databases are locked before the rows are narrowed to them, and an all-taken list is a skip, not a failure.
    assert source.index("try_lock_databases") < source.index("configs = planner.select_work(configs, work)")
    assert 'status="skipped",\n                         detail="every database of the work list' in source


def test_a_run_of_a_share_runs_each_collection_as_a_part_of_its_own_and_a_part_skips_rather_than_fails():
    source = _run_source()
    start = source.index("if work_id and not task_collection.strip():")
    parts = source[start:source.index("# Per-collection run lock", start)]
    assert "planner.cycle_parts(store.get_check_work_all(work_id))" in parts
    assert '{**_widgets, "task_collection": _part, "cycle_part": "true"}' in parts
    assert start < source.index("lock_store = StateStore.connect_lakebase(")     # before any lock is taken
    assert 'status = "skipped" if cycle_part else planner.locked_run_status(work_id)' in source
    assert "if cycle_part and not planner.database_keys(configs) & work:" in source


def test_the_check_shares_a_cycle_out_by_its_changes_unless_told_one_run_per_group():
    notebook = json.loads((NOTEBOOK.parent / "check_launch.ipynb").read_text())
    code = "\n".join(c["source"] if isinstance(c["source"], str) else "".join(c["source"]) for c in notebook["cells"])
    assert 'runs_by = widget("runs_by", "group")' in code
    assert 'hold_work_locks=runs_by == "group", share_locks=runs_by == "changes"' in code
    assert "launcher.cycle_requests(summary[\"per_collection\"], group_size" in code
    job = (NOTEBOOK.parents[2] / "resources" / "lakefed_ingest_check.yml").read_text()
    assert 'runs_by: "{{job.parameters.runs_by}}"' in job and '- name: runs_by\n          default: "group"' in job


def test_a_cycle_is_sized_by_the_jobs_own_tables_per_group_when_it_has_one():
    notebook = json.loads((NOTEBOOK.parent / "check_launch.ipynb").read_text())
    code = "\n".join(c["source"] if isinstance(c["source"], str) else "".join(c["source"]) for c in notebook["cells"])
    assert "store.schedule_tables_per_group(int(schedule_id)) if schedule_id else None" in code
    assert ss.schedule_tables_per_group_sql().count("%s") == 1
    cursor = _Cursor()
    cursor.execute = lambda sql, params=(): setattr(cursor, "rows", [(20_000,)])
    assert _store(cursor).schedule_tables_per_group(7) == 20_000
    cursor.execute = lambda sql, params=(): setattr(cursor, "rows", [])
    cursor.fetchone = lambda: (cursor.rows or [None])[0]
    assert _store(cursor).schedule_tables_per_group(7) is None
