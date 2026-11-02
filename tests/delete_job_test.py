"""Deleting an app job's tables (delete_job): which groups go, the run locks, what is deleted and kept, the targets."""
import contextlib
import re

import pytest

import delete_job as dj


class Column:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    """Answers the delete job's statements from a small in-memory state database."""

    def __init__(self, schedules, members, control, locked=(), selection=None):
        self.schedules, self.members, self.control, self.locked = schedules, members, control, set(locked)
        self.selection = selection      # job_selection rows by job id; None: a database below schema version 9
        self.executed, self.description, self.rowcount, self._result = [], [], -1, []

    def _answer(self, columns, rows):
        self.description = [Column(c) for c in columns]
        self._result = [tuple(r[c] for c in columns) for r in rows]

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql == dj.schedule_sql():
            self._answer(["id", "name", "deleted_at"], [s for s in self.schedules if s["id"] == params[0]])
        elif sql == dj.groups_sql():
            sid = params[0]
            live = {s["id"] for s in self.schedules if s["deleted_at"] is None}
            held = {m["task_collection"] for m in self.members
                    if m["schedule_id"] != sid and m["removed_at"] is None and m["schedule_id"] in live}
            mine = sorted({m["task_collection"] for m in self.members if m["schedule_id"] == sid} - held)
            self._answer(["task_collection"], [{"task_collection": g} for g in mine])
        elif sql == dj.lock_sql():
            self._result = [(params[0] not in self.locked,)]
        elif sql == dj.targets_sql():
            groups = set(params[0])
            key = lambda r: tuple(r[k].lower() for k in ("sink_catalog", "sink_schema", "sink_table"))
            theirs = {key(r) for r in self.control if r["task_collection"] not in groups}
            mine = {}
            for r in self.control:
                if r["task_collection"] in groups:
                    mine[(r["sink_catalog"], r["sink_schema"], r["sink_table"])] = key(r) in theirs
            self._answer(["sink_catalog", "sink_schema", "sink_table", "shared"],
                         [{"sink_catalog": c, "sink_schema": s, "sink_table": t, "shared": shared}
                          for (c, s, t), shared in sorted(mine.items())])
        elif sql == dj.selection_table_sql():
            self._result = [(self.selection is not None,)]
        elif sql == dj.delete_selection_sql():
            self.rowcount = (self.selection or {}).get(params[0], 0)
        elif sql == dj.expired_jobs_sql():
            self._answer(["id", "name", "drop_targets"], [s for s in self.schedules if _expired(s)])
        elif sql == dj.expired_check_sql():
            self._result = [(s["drop_targets"],) for s in self.schedules if s["id"] == params[0] and _expired(s)]
        elif sql == dj.finished_sql():
            for s in self.schedules:
                if s["id"] == params[0]:
                    s["finished"] = True
        elif sql.startswith("delete from"):
            table = re.match(r"delete from lakefed_ingest\.(\w+)", sql).group(1)
            self.rowcount = {"control": sum(r["task_collection"] in params[0] for r in self.control)}.get(table, 2)
        else:
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0]


def schedule(sid, deleted=True, name=None, expired=False, drop_targets=False, finished=False):
    return {"id": sid, "name": name or f"Job {sid}", "deleted_at": "2026-10-29" if deleted else None,
            "expired": expired, "drop_targets": drop_targets, "finished": finished}


def _expired(job):
    return job["expired"] and not job["finished"]


def member(sid, group, removed=False):
    return {"schedule_id": sid, "task_collection": group, "removed_at": "2026-10-29" if removed else None}


def row(group, table, schema="sales", catalog="main"):
    return {"task_collection": group, "sink_catalog": catalog, "sink_schema": schema, "sink_table": table}


def statements(cur):
    return [sql for sql, _ in cur.executed]


def test_a_deleted_jobs_groups_rows_go_in_order_under_each_groups_run_lock():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True), member(7, "g2", removed=True)],
                     [row("g1", "a"), row("g1", "b"), row("g2", "c")])
    result = dj.delete_rows(cur, 7)
    assert result["groups"] == ["g1", "g2"] and result["deleted"]["control"] == 3
    assert [p for sql, p in cur.executed if sql == dj.lock_sql()] == [("lakefed_ingest:g1",), ("lakefed_ingest:g2",)]
    deletes = [re.match(r"delete from lakefed_ingest\.(\w+)", s).group(1) for s in statements(cur) if s.startswith("delete")]
    assert deletes == ["reseed_queue", "checkpoint", "database_check", "check_work", "rollup_database",
                       "rollup_group", "control"]
    assert all(p == (["g1", "g2"],) for sql, p in cur.executed if sql.startswith("delete"))
    locks_end = max(i for i, s in enumerate(statements(cur)) if s == dj.lock_sql())
    assert locks_end < min(i for i, s in enumerate(statements(cur)) if s.startswith("delete"))


def test_run_history_and_the_change_log_are_kept():
    touched = " ".join(sql for sql, _ in dj.DELETES)
    for kept in ("ingest_run", "ingest_event", "control_change", "schedule"):
        assert f"lakefed_ingest.{kept} " not in touched and not touched.endswith(f"lakefed_ingest.{kept}")
    assert "drop schema" not in dj.drop_sql("`a`.`b`.`c`").lower()


def test_a_group_that_is_loading_refuses_the_lot():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True), member(7, "g2", removed=True)],
                     [row("g1", "a"), row("g2", "c")], locked={"lakefed_ingest:g2"})
    with pytest.raises(dj.RunGoing, match="g2"):
        dj.delete_rows(cur, 7)
    assert not any(s.startswith("delete") for s in statements(cur))


def test_a_job_that_is_not_deleted_or_does_not_exist_is_refused():
    cur = FakeCursor([schedule(7, deleted=False)], [member(7, "g1")], [row("g1", "a")])
    with pytest.raises(ValueError, match="isn't deleted"):
        dj.delete_rows(cur, 7)
    with pytest.raises(ValueError, match="doesn't exist"):
        dj.delete_rows(cur, 8)
    assert not any(s.startswith("delete") for s in statements(cur))


def test_a_group_now_in_another_live_job_stays():
    cur = FakeCursor([schedule(7), schedule(8, deleted=False)],
                     [member(7, "g1", removed=True), member(7, "g2", removed=True), member(8, "g2")],
                     [row("g1", "a"), row("g2", "c")])
    assert dj.delete_rows(cur, 7)["groups"] == ["g1"]


def test_a_group_of_another_deleted_job_or_removed_from_one_still_goes():
    cur = FakeCursor([schedule(7), schedule(8), schedule(9, deleted=False)],
                     [member(7, "g1", removed=True), member(8, "g1", removed=True), member(9, "g1", removed=True)],
                     [row("g1", "a")])
    assert dj.delete_rows(cur, 7)["groups"] == ["g1"]


def test_a_job_with_no_groups_left_deletes_nothing():
    cur = FakeCursor([schedule(7), schedule(8, deleted=False)], [member(7, "g1", removed=True), member(8, "g1")],
                     [row("g1", "a")])
    result = dj.delete_rows(cur, 7)
    assert result == {"job": "Job 7", "groups": [], "deleted": {}, "targets": {"drop": [], "kept": []}}
    assert not any(s.startswith("delete") or s == dj.lock_sql() for s in statements(cur))


def test_targets_another_groups_rows_also_write_are_kept_and_named():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)],
                     [row("g1", "a"), row("g1", "fan_in"), row("g1", "fan_in"), row("other", "FAN_IN"),
                      row("g1", "my-table", schema="01-sales")])
    targets = dj.delete_rows(cur, 7)["targets"]
    assert targets == {"drop": ["`main`.`01-sales`.`my-table`", "`main`.`sales`.`a`"], "kept": ["`main`.`sales`.`fan_in`"]}
    assert dj.drop_sql(targets["drop"][0]) == "DROP TABLE IF EXISTS `main`.`01-sales`.`my-table`"


def test_the_targets_query_compares_names_without_case_and_reads_every_other_group():
    sql = dj.targets_sql()
    assert "lower(sink_table)" in sql and "not (task_collection = any(%s))" in sql


def test_the_summary_counts_what_went_and_names_what_was_kept_or_failed():
    result = {"job": "Hourly", "groups": ["g1", "g2"], "deleted": {"control": 5, "checkpoint": 4},
              "targets": {"drop": ["`m`.`s`.`a`"], "kept": ["`m`.`s`.`b`"]}}
    assert dj.summary(result, ["`m`.`s`.`a`"]) == {
        "job": "Hourly", "groups": 2, "tables": 5, "deleted": {"control": 5, "checkpoint": 4}, "dropped": 1,
        "kept": 1, "kept_names": ["`m`.`s`.`b`"], "drop_failed": 0, "drop_failed_names": {}}
    many = {f"`m`.`s`.`t{i}`": "denied" for i in range(25)}
    message = dj.failure_message(many)
    assert message.startswith("the job's tables are deleted, but 25 target table(s) could not be dropped")
    assert "`m`.`s`.`t19`: denied" in message and "`m`.`s`.`t20`" not in message and message.endswith("and 5 more (the run's output lists them)")


def test_what_the_job_was_made_from_goes_with_it_where_the_database_keeps_it():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)], [row("g1", "a")], selection={7: 3, 8: 2})
    result = dj.delete_rows(cur, 7)
    assert result["deleted"]["job_selection"] == 3
    assert [p for sql, p in cur.executed if sql == dj.delete_selection_sql()] == [(7,)]
    assert statements(cur)[-2:] == [dj.delete_selection_sql(), dj.finished_sql()]    # in the same transaction, last
    # A job with no groups left still forgets it; a database below version 9 has nothing to forget.
    cur = FakeCursor([schedule(7)], [], [], selection={7: 1})
    assert dj.delete_rows(cur, 7)["deleted"] == {"job_selection": 1}
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)], [row("g1", "a")])
    assert "job_selection" not in dj.delete_rows(cur, 7)["deleted"]
    assert dj.delete_selection_sql() not in statements(cur)


class PurgeCursor:
    """The delete job's purge statements over drafts in memory: {discovery_id: {"databases": [...], "tables": n,
    "draft": still marked}}."""

    def __init__(self, purgeable, drafts):
        self.purgeable, self.drafts, self.executed, self.rowcount, self._result = purgeable, drafts, [], -1, []

    def execute(self, sql, params=()):
        self.executed.append(sql)
        if sql == dj.purgeable_drafts_sql():
            self._result = [(i,) for i in self.purgeable]
        elif sql == dj.purge_discovery_sql():
            gone = [i for i in params[0] if self.drafts[i]["draft"]]
            self._result = [(i,) for i in gone for _ in self.drafts[i]["databases"]]
            self.rowcount = len(self._result)
        elif sql == dj.purge_tables_sql():
            self.rowcount = sum(self.drafts[i]["tables"] for i in params[0])
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self._result


def test_the_purge_deletes_the_drafts_discarded_or_left_and_their_tables():
    drafts = {"d1": {"databases": ["a", "b"], "tables": 30, "draft": True},
              "d2": {"databases": ["c"], "tables": 5, "draft": False}}      # a Create took it meanwhile: kept
    cur = PurgeCursor(["d1", "d2"], drafts)
    assert dj.purge_drafts(cur, 3600) == {"drafts": 1, "databases": 2, "tables": 30}
    assert cur.executed == [dj.purgeable_drafts_sql(), dj.purge_discovery_sql(), dj.purge_tables_sql()]


def test_a_purge_with_nothing_to_purge_deletes_nothing():
    cur = PurgeCursor([], {})
    assert dj.purge_drafts(cur) == {"drafts": 0, "databases": 0, "tables": 0}
    assert cur.executed == [dj.purgeable_drafts_sql()]


def test_the_purge_takes_only_marked_drafts():
    assert "listing ? 'draft'" in dj.purgeable_drafts_sql() and "listing ? 'draft'" in dj.purge_discovery_sql()
    assert "bool_or(listing ? 'discarded')" in dj.purgeable_drafts_sql()


class TableCursor:
    """Answers the per-table statements (delete_table_rows) from an in-memory state: the job's schedules and members,
    the control rows with their checkpoints and reseed rows, and the groups whose run locks are held."""

    def __init__(self, schedules, members, control, checkpoints=(), reseeds=(), locked=()):
        self.schedules, self.members, self.control = schedules, members, control
        self.state = {"checkpoint": list(checkpoints), "reseed_queue": list(reseeds)}
        self.locked, self.recounted = set(locked), []
        self.executed, self.description, self.rowcount, self._result = [], [], -1, []

    def _answer(self, columns, rows):
        self.description = [Column(c) for c in columns]
        self._result = [tuple(r[c] for c in columns) for r in rows]

    def execute(self, sql, params=()):
        self.executed.append(sql)
        if sql == dj.schedule_sql():
            self._answer(["id", "name", "deleted_at"], [s for s in self.schedules if s["id"] == params[0]])
        elif sql == dj.job_groups_sql():
            sid = params[0]
            live = {s["id"] for s in self.schedules if s["deleted_at"] is None}
            held = {m["task_collection"] for m in self.members
                    if m["schedule_id"] != sid and m["removed_at"] is None and m["schedule_id"] in live}
            mine = sorted({m["task_collection"] for m in self.members if m["schedule_id"] == sid and m["removed_at"] is None}
                          - held)
            self._answer(["task_collection"], [{"task_collection": g} for g in mine])
        elif sql == dj.table_rows_sql():
            wanted = set(params[0])
            self._answer(["id", "task_collection", "src_database", "sink_catalog", "sink_schema", "sink_table"],
                         [r for r in self.control if r["id"] in wanted])
        elif sql == dj.lock_sql():
            self._result = [(params[0] not in self.locked,)]
        elif sql in dict(dj.TABLE_DELETES).values():
            name = next(n for n, s in dj.TABLE_DELETES if s == sql)
            ids = set(params[0])
            if name == "control":
                self.control = [r for r in self.control if r["id"] not in ids]
                self.rowcount = len(ids)
            else:
                kept = [r for r in self.state[name] if r["control_id"] not in ids]
                self.rowcount = len(self.state[name]) - len(kept)
                self.state[name] = kept
        elif sql in (dj.rollup_database_rewrite_sql(), dj.rollup_group_rewrite_sql(), dj.rollup_group_lock_sql()):
            self.recounted.append(sql)
        else:
            raise AssertionError(f"unexpected statement: {sql[:80]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def table_control(control_id, group, table, database="db1"):
    return {"id": control_id, "task_collection": group, "src_database": database, "sink_catalog": "main",
            "sink_schema": "lakehouse", "sink_table": f"{group}_{table}"}


def _table_fixture(**overrides):
    kwargs = dict(
        schedules=[schedule(1, deleted=False, name="Hourly"), schedule(2, deleted=False, name="Other")],
        members=[member(1, "sales"), member(1, "marketing"), member(2, "marketing"), member(2, "finance")],
        control=[table_control(10, "sales", "orders"), table_control(11, "sales", "items"),
                 table_control(12, "marketing", "leads"), table_control(20, "finance", "gl")],
        checkpoints=[{"control_id": 10}, {"control_id": 11}, {"control_id": 12}],
        reseeds=[{"control_id": 10}],
    )
    kwargs.update(overrides)
    return TableCursor(**kwargs)


def test_removing_a_table_deletes_only_its_rows_and_state_and_keeps_its_target():
    cur = _table_fixture()
    result = dj.delete_table_rows(cur, 1, [10])
    assert [r["id"] for r in cur.control] == [11, 12, 20]
    assert [r["control_id"] for r in cur.state["checkpoint"]] == [11, 12]
    assert cur.state["reseed_queue"] == []
    assert result["deleted"] == {"reseed_queue": 1, "checkpoint": 1, "control": 1}
    assert result["targets"] == {"drop": [], "kept": ["`main`.`lakehouse`.`sales_orders`"]}
    assert not any("drop" in sql.lower() for sql in cur.executed)


def test_a_group_that_is_loading_refuses_the_removal_and_deletes_nothing():
    cur = _table_fixture(locked={dj.collection_lock_key("sales")})
    with pytest.raises(dj.RunGoing):
        dj.delete_table_rows(cur, 1, [10])
    assert [r["id"] for r in cur.control] == [10, 11, 12, 20]
    assert len(cur.state["checkpoint"]) == 3 and len(cur.state["reseed_queue"]) == 1


def test_a_table_whose_group_another_live_job_holds_is_refused_and_nothing_goes():
    cur = _table_fixture()
    with pytest.raises(ValueError, match="marketing"):
        dj.delete_table_rows(cur, 1, [12])
    assert len(cur.control) == 4


def test_a_deleted_job_no_ids_or_an_unknown_job_are_refused():
    with pytest.raises(ValueError, match="deleted"):
        dj.delete_table_rows(_table_fixture(schedules=[schedule(1, deleted=True, name="Hourly")]), 1, [10])
    with pytest.raises(ValueError, match="no control ids"):
        dj.delete_table_rows(_table_fixture(), 1, [])
    with pytest.raises(ValueError, match="doesn't exist"):
        dj.delete_table_rows(_table_fixture(), 99, [10])


def test_the_databases_and_groups_it_touched_are_recounted_after_the_rows_go():
    cur = _table_fixture()
    dj.delete_table_rows(cur, 1, [10, 11])
    assert cur.recounted == [dj.rollup_group_lock_sql(), dj.rollup_database_rewrite_sql(), dj.rollup_group_rewrite_sql()]


def test_ids_already_gone_are_reported_as_missing_and_nothing_else_goes():
    cur = _table_fixture()
    result = dj.delete_table_rows(cur, 1, [10, 999])
    assert result["missing"] == [999] and result["control_ids"] == [10]
    assert [r["id"] for r in cur.control] == [11, 12, 20]


def test_a_purge_marks_the_job_finished_in_the_same_transaction_as_its_rows():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)], [row("g1", "a")])
    dj.delete_rows(cur, 7)
    assert statements(cur)[-1] == dj.finished_sql()
    assert cur.schedules[0]["finished"] and [p for sql, p in cur.executed if sql == dj.finished_sql()] == [(7,)]


def test_a_job_with_no_groups_left_is_still_marked_finished():
    cur = FakeCursor([schedule(7)], [], [])
    dj.delete_rows(cur, 7)
    assert cur.schedules[0]["finished"] and statements(cur)[-1] == dj.finished_sql()


def test_a_refused_purge_marks_nothing_finished():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)], [row("g1", "a")], locked={"lakefed_ingest:g1"})
    with pytest.raises(dj.RunGoing):
        dj.delete_rows(cur, 7)
    assert not cur.schedules[0]["finished"] and dj.finished_sql() not in statements(cur)


def test_the_finished_mark_is_the_flag_the_app_reads_and_the_job_is_locked_while_it_is_read():
    sql = dj.finished_sql()
    assert "'{delete_finished}'" in sql and "'true'::jsonb" in sql and sql.endswith("where id = %s")
    assert dj.schedule_sql().endswith("for update") and "for update" in dj.expired_check_sql()


def test_the_expired_jobs_are_the_deleted_ones_past_retention_and_not_finished():
    sql = dj.expired_jobs_sql()
    assert "deleted_at <= now() - make_interval(days => %s)" in sql
    assert "not coalesce((options ->> 'delete_finished')::boolean, false)" in sql and "delete_drop_targets" in sql
    cur = FakeCursor([schedule(7, expired=True, drop_targets=True), schedule(8),
                      schedule(9, expired=True, finished=True)], [], [])
    assert dj.expired_jobs(cur, 7) == [{"id": 7, "name": "Job 7", "drop_targets": True}]
    assert cur.executed[-1] == (sql, (7,))


def test_a_job_whose_retention_has_not_run_out_is_not_purged():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)], [row("g1", "a")])
    assert dj.delete_expired(cur, 7, 7) is None
    assert not any(s.startswith("delete") or s == dj.finished_sql() for s in statements(cur))


def test_an_expired_job_is_purged_and_says_whether_it_asked_for_its_tables():
    cur = FakeCursor([schedule(7, expired=True, drop_targets=True)], [member(7, "g1", removed=True)],
                     [row("g1", "a")])
    result = dj.delete_expired(cur, 7, 7)
    assert result["groups"] == ["g1"] and result["drop_targets"] is True
    assert result["targets"]["drop"] == ["`main`.`sales`.`a`"] and cur.schedules[0]["finished"]
    assert [p for sql, p in cur.executed if sql == dj.expired_check_sql()] == [(7, 7)]
    cur = FakeCursor([schedule(7, expired=True)], [member(7, "g1", removed=True)], [row("g1", "a")])
    assert dj.delete_expired(cur, 7, 7)["drop_targets"] is False


def _dropper(log, failing=()):
    def drop(tables):
        log.extend(tables)
        return {table: "denied" for table in tables if table in failing}
    return drop


def test_the_daily_purge_with_nothing_expired_purges_nothing():
    cur = FakeCursor([schedule(7)], [member(7, "g1", removed=True)], [row("g1", "a")])
    log = []
    assert dj.purge_expired(lambda: contextlib.nullcontext(cur), 7, _dropper(log)) == {
        "listed": 0, "purged": [], "waiting": [], "failed": {}}
    assert log == []


def test_a_job_that_is_loading_waits_for_the_next_daily_run_and_the_others_go_on():
    schedules = [schedule(7, expired=True), schedule(8, expired=True, drop_targets=True)]
    cur = FakeCursor(schedules, [member(7, "g1", removed=True), member(8, "g2", removed=True)],
                     [row("g1", "a"), row("g2", "b")], locked={"lakefed_ingest:g1"})
    log = []
    purged = dj.purge_expired(lambda: contextlib.nullcontext(cur), 7, _dropper(log))
    assert purged["listed"] == 2 and purged["failed"] == {}
    assert [entry["job"] for entry in purged["waiting"]] == ["Job 7"] and "g1" in purged["waiting"][0]["reason"]
    assert [entry["job"] for entry in purged["purged"]] == ["Job 8"] and purged["purged"][0]["dropped"] == 1
    assert log == ["`main`.`sales`.`b`"] and not schedules[0]["finished"] and schedules[1]["finished"]


def test_a_target_table_that_will_not_drop_is_named_and_the_rest_of_the_purge_goes_on():
    schedules = [schedule(7, expired=True, drop_targets=True), schedule(8, expired=True, drop_targets=True)]
    cur = FakeCursor(schedules, [member(7, "g1", removed=True), member(8, "g2", removed=True)],
                     [row("g1", "a"), row("g2", "b")])
    log = []
    purged = dj.purge_expired(lambda: contextlib.nullcontext(cur), 7,
                              _dropper(log, failing={"`main`.`sales`.`a`"}))
    assert purged["failed"] == {"`main`.`sales`.`a`": "denied"}
    assert [entry["job"] for entry in purged["purged"]] == ["Job 7", "Job 8"]
    assert purged["purged"][0]["drop_failed"] == 1 and purged["purged"][0]["dropped"] == 0
    assert purged["purged"][1]["dropped"] == 1 and all(s["finished"] for s in schedules)


def test_a_job_restored_after_the_list_was_read_is_skipped():
    cur = FakeCursor([schedule(7, expired=True)], [member(7, "g1", removed=True)], [row("g1", "a")])
    opened = []

    @contextlib.contextmanager
    def transaction():
        if opened:
            cur.schedules[0]["expired"] = False       # a Restore committed between the list and the purge
        opened.append(True)
        yield cur

    purged = dj.purge_expired(transaction, 7, _dropper([]))
    assert purged["listed"] == 1 and purged["purged"] == [] and not cur.schedules[0]["finished"]
