"""The statements the app runs to keep the monitoring rollups exact after its own edits, against a fake cursor, and
the read of a group with no rollup row yet (counted from its control rows)."""
import main
import queries as q
import rollup


class FakeCursor:
    def __init__(self, rows=()):
        self.executed = []
        self.rows = list(rows)

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_a_write_rewrites_each_touched_database_once_then_its_groups():
    cur = FakeCursor()
    main._rewrite_rollups(cur, [("c2", "db1"), ("c1", None), ("c1", "db1"), ("c1", "db1")])
    assert cur.executed == [
        (q.rollup_database_rewrite_sql(), (["c1", "c1", "c2"], ["", "db1", "db1"])),
        (q.rollup_group_rewrite_sql(), (["c1", "c2"],)),
    ]


def test_a_large_write_is_chunked_and_the_groups_are_written_last():
    cur = FakeCursor()
    main._rewrite_rollups(cur, [("c1", f"db{i:03d}") for i in range(450)])
    sizes = [len(params[1]) for sql, params in cur.executed[:-1]]
    assert sizes == [200, 200, 50]
    assert cur.executed[-1] == (q.rollup_group_rewrite_sql(), (["c1"],))


def test_nothing_is_written_when_nothing_changed():
    cur = FakeCursor()
    main._rewrite_rollups(cur, [])
    main._rewrite_rollups_for_ids(cur, [])
    main._rewrite_rollups_for_groups(cur, set())
    assert cur.executed == []


def test_an_edit_by_id_or_by_group_rewrites_the_databases_those_rows_are_in():
    cur = FakeCursor(rows=[("c1", "db1"), ("c1", "")])
    main._rewrite_rollups_for_ids(cur, [7, 9])
    assert cur.executed[0] == (q.control_databases_sql("ids"), ([7, 9],))
    assert cur.executed[1] == (q.rollup_database_rewrite_sql(), (["c1", "c1"], ["", "db1"]))
    cur = FakeCursor(rows=[("c2", "db9")])
    main._rewrite_rollups_for_groups(cur, {"c2"})
    assert cur.executed[0] == (q.control_databases_sql("groups"), (["c2"],))


def test_a_group_with_no_rollup_row_is_counted_from_its_control_rows(monkeypatch):
    scanned = dict(server="s1", src_connection=None, src_host="h", task_collection="c2", src_database="d",
                   instance_label=None, tables=4, no_sla_tables=0, state_current=2, state_late=2, state_first_load=0,
                   state_failing=0, oldest_success_at=None, newest_success_at=None, newest_any_success_at=None)
    rolled = dict(server="s1", src_connection=None, src_host=None, task_collection="c1", src_database="a",
                  instance_label=None, tables=1, no_sla_tables=0, state_loaded=1, late_database=False,
                  state_first_load=0, state_failing=0, oldest_success_at=None, newest_success_at=None,
                  newest_any_success_at=None)
    answers = {q.rollup_states_sql(): [rolled], q.rollup_group_names_sql(): [dict(task_collection="c1")],
               q.control_group_names_sql(): [dict(task_collection="c1"), dict(task_collection="c2")],
               q.database_states_for_groups_sql(): [scanned]}
    asked = []
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): asked.append((sql, params)) or answers[sql])
    monkeypatch.setattr(main, "CONTROL_GROUPS_S", 0)
    rows = main._load_states()
    assert asked[-1] == (q.database_states_for_groups_sql(), (["c2"],))
    by_group = {r["task_collection"]: r for r in rows}
    assert (by_group["c1"]["state_loaded"], by_group["c1"]["late_database"]) == (1, False)
    assert (by_group["c2"]["state_loaded"], by_group["c2"]["late_database"]) == (4, True)
    assert "state_current" not in by_group["c2"] and "state_late" not in by_group["c2"]
    assert rollup.groups(rows)["c2"]["late_databases"] == 1
