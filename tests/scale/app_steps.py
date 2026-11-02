"""The operations app's steps at scale: creating an ingestion job by reference to a discovery (in the request, or
database by database in the background, as the route chooses by the count), and uploading a CSV of tables. They call
the app's own planning (new_job.plan, by_database.plan_database, read_upload) and its write helpers
(main._insert_controls, main._log, main._keep_selection, ...) with the recording cursor from lakebase_fake.
no_connections() is in force for all of it, so a step that tried to reach a database would raise.

What is not measured here: the reads the route does from Lakebase (the selection's rows, the discovered rows) and the
request's own JSON handling. Those come back as rows built in Python, and the report says so.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import paths  # noqa: F401  (sys.path)
import by_database
import lakebase_fake
import main as app
import new_job as nj
import queries as q
import server_catalog
import simulation as sim
import steps

CREATE_SETTINGS = {"sink_catalog": "lab", "sink_schema": "scale", "sink_table": "{database}_{table}"}
SCALE_USER = "scale@example.com"
SCALE_JOB = "scale-job"
SCALE_BATCH = "scale-batch"
WHOLE_SERVER_PART = ("server", sim.connection_spec()["src_connection"], None, None, None)


@dataclass
class CreateRun:
    """One create by reference: the planned lines, the groups, and what the writes sent."""
    tables: int
    groups: int
    csv_bytes: int
    errors: List[str]
    csv_s: float
    plan_s: float
    write_s: float
    summary: dict
    connection: lakebase_fake.RecordingConnection


@dataclass
class ByDatabaseRun:
    """One create by database: the check pass and the write pass over every database, and what the writes sent. read_s
    is the time spent reading each database's rows (the harness re-runs discovery's reads for them), which is in neither
    pass's time."""
    databases: int
    tables: int
    groups: List[str]
    errors: List[str]
    check_s: float
    write_s: float
    read_s: float
    connection: lakebase_fake.RecordingConnection


@dataclass
class UploadRun:
    """One upload of a CSV of tables: what read_upload made of it, and what the upload loop sent."""
    lines: int
    errors: List[str]
    csv_bytes: int
    read_s: float
    write_s: float
    connection: Optional[lakebase_fake.RecordingConnection]


def _discovered_fields(spec: dict, database: str, table: dict) -> dict:
    """A discovered table as the discovery selection reads it into a CSV line (DISCOVERY_COLUMNS)."""
    return {"src_connection": spec["src_connection"], "src_host": None, "src_port": None, "secret_key": None,
            "src_database": database, "src_schema": table["src_schema"], "src_table": table["src_table"],
            "select_list": table.get("select_list"), "primary_key": table.get("primary_key"),
            "cursor_column": table.get("cursor_column"), "load_type": table.get("load_type")}


def discovered_rows(catalog: server_catalog.ServerCatalog, limit: Optional[int] = None) -> List[dict]:
    """The discovered tables of a server as rows of DISCOVERY_COLUMNS (what a selection by discovery reads)."""
    spec = sim.connection_spec()
    rows: List[dict] = []
    steps.discover(catalog, limit=limit,
                   on_found=lambda database, found: rows.extend(_discovered_fields(spec, database, t) for t in found))
    return rows


def _control_ids(params: tuple) -> List[tuple]:
    """insert_controls_sql returns (id, sink key) for each row written: ids are numbered in the order given."""
    width = len(q.CONTROL_INSERT_COLUMNS)
    return [(number, *q.control_sink_key(params[at:at + width]))
            for number, at in enumerate(range(0, len(params), width), start=1)]


def _answers(sql: str, params: tuple) -> List[tuple]:
    """What the reads and returning clauses of the create path give back: every collection is free, every control row
    is written, the schedule gets id 1, and no sink or database collides."""
    if "pg_try_advisory_xact_lock" in sql:
        return [(key, True) for key in params[0]] if "unnest(" in sql else [(True,)]
    if sql.startswith(f"insert into {q.SCHEMA}.control ("):
        return _control_ids(params)
    if sql.startswith(f"insert into {q.SCHEMA}.schedule ("):
        return [(1,)]
    if sql.startswith(f"insert into {q.SCHEMA}.schedule_member ("):
        return [(1,)]
    return []


def _write_job(cur, plan: dict, selected: dict, name: str, user: str, batch: str) -> None:
    """The writes create_ingestion_job makes in its transaction, in its order, minus the schedule check (which reads the
    Databricks jobs) and the cron offsets (each member starts at 0)."""
    rows = [line["row"] for line in plan["lines"]]
    app._lock_collections(cur, plan["groups"])
    cur.execute(q.insert_schedule_sql(), (None,) * (len(q.SCHEDULE_FIELDS) + 1))
    schedule_id = cur.fetchone()[0]
    app._insert_controls(cur, rows, user, batch)
    app._refuse_mixed_sinks(cur, rows)
    app._refuse_split_databases(cur, rows)
    for group in plan["groups"]:
        cur.execute(q.insert_member_sql(), (schedule_id, group, 0))
    app._log_lines(cur, [(user, "schedule_create", None, group, "schedule", None, name, None, batch)
                         for group in plan["groups"]])
    app._keep_selection(cur, schedule_id, selected, user)
    app._touch(cur, user, [schedule_id])


def create_by_reference(catalog: server_catalog.ServerCatalog, limit: Optional[int] = None,
                        groups_per_server: int = nj.DEFAULT_GROUPS_PER_SERVER) -> CreateRun:
    """Create a job whose tables are a whole server's discovery: the CSV the selection makes, the plan, and the writes.
    Runs under no_connections()."""
    with lakebase_fake.no_connections():
        rows = discovered_rows(catalog, limit=limit)
        started = time.perf_counter()
        text = nj.csv_of(list(nj.DISCOVERY_COLUMNS), rows)
        csv_s = time.perf_counter() - started
        started = time.perf_counter()
        plan = nj.plan(SCALE_JOB, text, CREATE_SETTINGS, groups_per_server, taken_groups=set())
        plan_s = time.perf_counter() - started
        summary = nj.summary(plan)
        connection = lakebase_fake.RecordingConnection(answers=_answers)
        selected = {"parts": [WHOLE_SERVER_PART], "kind": "discovery", "source_id": "scale-discovery"}
        started = time.perf_counter()
        if not plan["errors"]:
            with connection.transaction() as cur:
                _write_job(cur, plan, selected, SCALE_JOB, SCALE_USER, SCALE_BATCH)
        write_s = time.perf_counter() - started
    return CreateRun(tables=len(plan["lines"]), groups=len(plan["groups"]), csv_bytes=len(text.encode()),
                     errors=list(plan["errors"]), csv_s=csv_s, plan_s=plan_s, write_s=write_s, summary=summary,
                     connection=connection)


def _database_rows(server: sim.SimulatedServer, spec: dict, database: str) -> List[dict]:
    """One database's discovered rows, as a selection reads them from Lakebase ({server, src_database, src_schema,
    src_table, line, fields}). The discovery's own reads of that database make them, one database at a time, so a
    server's tables are never held whole. An unopenable database has none."""
    with sim.discovery_boundary(server):
        try:
            found = sim.discover_one(server, spec, database)
        except RuntimeError:
            return []
    return [{"server": spec["src_connection"], "src_database": database, "src_schema": table["src_schema"],
             "src_table": table["src_table"], "line": None, "fields": _discovered_fields(spec, database, table)}
            for table in found]


SOURCE_KIND = "discovery"
CADENCE_MINUTES = 60
TIMEZONE = "UTC"
TASK_ID = 1


def _listed_databases(catalog: server_catalog.ServerCatalog) -> List[str]:
    """The databases the source lists: the unopenable ones are not listed, so the page reads never see them."""
    unopenable = set(catalog.unopenable_names())
    return [name for name in catalog.database_names() if name not in unopenable]


def _record_pages(connection: lakebase_fake.RecordingConnection, databases: int) -> None:
    """The page reads one pass makes over a source's databases: a page is full until the last one, so N databases take
    N // DATABASE_PAGE + 1 pages (queries.source_databases_after_sql)."""
    connection.record(q.source_databases_after_sql(SOURCE_KIND), databases,
                      count=databases // by_database.DATABASE_PAGE + 1)


def _member_answers(members: set):
    """What the member inserts give back: a group the job has no member for yet is added and returned, and a group it
    has is skipped. Both the one-row insert (the database-by-database write) and the set insert (the packed write)."""
    def answers(sql: str, params: tuple) -> List[tuple]:
        if sql.startswith(f"insert into {q.SCHEMA}.schedule_member ("):
            if "unnest(" in sql:
                added = [group for group in params[1] if group not in members]
                members.update(added)
                return [(group,) for group in added]
            if params[1] in members:
                return []
            members.add(params[1])
        return _answers(sql, params)
    return answers


def _open_job(connection: lakebase_fake.RecordingConnection, selected: dict) -> int:
    """The write before any database: the job's schedule, what it was chosen from, its touch, and the task's link to the
    job, in one transaction. Returns the schedule's id."""
    with connection.transaction() as cur:
        cur.execute(q.insert_schedule_sql(), (None,) * (len(q.SCHEDULE_FIELDS) + 1))
        schedule_id = cur.fetchone()[0]
        app._keep_selection(cur, schedule_id, selected, SCALE_USER)
        app._touch(cur, SCALE_USER, [schedule_id])
        cur.execute(q.task_job_sql(), (schedule_id, TASK_ID))
    return schedule_id


def _by_database_setup(catalog: server_catalog.ServerCatalog):
    """What both by-database paths start from: the source, the listed databases, the groups by server, the columns and
    the selection the job keeps."""
    spec = sim.connection_spec()
    listed = _listed_databases(catalog)
    groups_of = by_database.groups_by_server(
        [{"server": spec["src_connection"], "databases": len(listed),
          "tables": len(listed) * catalog.tables_per_database}])
    selected = {"parts": [WHOLE_SERVER_PART], "kind": "discovery", "source_id": "scale-discovery"}
    return spec, sim.SimulatedServer(catalog), listed, groups_of, list(nj.DISCOVERY_COLUMNS), selected


def create_by_database(catalog: server_catalog.ServerCatalog) -> ByDatabaseRun:
    """The by-database create as it ran before the writes were packed, kept as the baseline: a check pass that plans
    every database and writes nothing, then the write pass, one transaction per database after the job's own. Each
    database's rows are one statement in each pass, and the page listing is read in each pass too. Runs under
    no_connections()."""
    spec, server, listed, groups_of, columns, selected = _by_database_setup(catalog)
    opened = len(listed)
    reads = [0.0]
    members: set = set()
    connection = lakebase_fake.RecordingConnection(answers=_member_answers(members))

    def rows_of(name: str) -> List[dict]:
        started = time.perf_counter()
        found = _database_rows(server, spec, name)
        reads[0] += time.perf_counter() - started
        connection.record(q.database_rows_sql(SOURCE_KIND), len(found))
        return found

    with lakebase_fake.no_connections():
        started, reads_before = time.perf_counter(), reads[0]
        errors, tables, groups = [], 0, set()
        _record_pages(connection, opened)
        for name in listed:
            rows = rows_of(name)
            if not rows:
                continue
            plan = by_database.plan_database(SCALE_JOB, CREATE_SETTINGS, rows, columns, groups_of, set(), set())
            errors += plan["errors"]
            tables += len(plan["lines"])
            groups.update(plan["groups"])
        check_s = time.perf_counter() - started - (reads[0] - reads_before)

        written = 0
        started, reads_before = time.perf_counter(), reads[0]
        if not errors:
            schedule_id = _open_job(connection, selected)
            _record_pages(connection, opened)
            for name in listed:
                rows = rows_of(name)
                if not rows:
                    continue
                plan = by_database.plan_database(SCALE_JOB, CREATE_SETTINGS, rows, columns, groups_of, set(), set())
                database_groups = sorted(plan["groups"])
                with connection.transaction() as cur:
                    app._lock_collections(cur, database_groups)
                    cur.execute(q.job_database_sinks_sql(), (database_groups, name))
                    cur.fetchall()
                    rows_todo = [line["row"] for line in plan["lines"]]
                    app._insert_controls(cur, rows_todo, SCALE_USER, SCALE_BATCH)
                    app._refuse_mixed_sinks(cur, rows_todo)
                    app._refuse_split_databases(cur, rows_todo)
                    for group in database_groups:
                        cur.execute(q.insert_member_if_absent_sql(), (schedule_id, group, 0))
                        if cur.fetchone():
                            app._log(cur, SCALE_USER, "schedule_create", None, group, "schedule", None, SCALE_JOB,
                                     None, SCALE_BATCH)
                    written += 1
                    progress = {"stage": "writing", "databases_total": opened, "databases_written": written,
                                "tables_written": tables}
                    cur.execute(q.task_progress_json_sql(), (json.dumps(progress), tables, TASK_ID))
            with connection.transaction() as cur:
                app._task_written(cur, TASK_ID, {"id": schedule_id}, schedule_id, tables)
        write_s = time.perf_counter() - started - (reads[0] - reads_before)
    return ByDatabaseRun(databases=written, tables=tables, groups=sorted(groups), errors=list(errors), check_s=check_s,
                         write_s=write_s, read_s=reads[0], connection=connection)


def create_by_chunk(catalog: server_catalog.ServerCatalog) -> ByDatabaseRun:
    """The by-database create as the route now runs it: the databases are read a batch at a time (one statement for the
    batch's tables) and planned in the check, and the writes are packed into transactions of up to
    TABLES_PER_TRANSACTION tables, each written by app._write_chunk (the writer the route calls). Up to PLAN_ONCE_TABLES
    tables the write takes the check's plans, so the databases are read once; above it they are read again. Runs under
    no_connections()."""
    spec, server, listed, groups_of, columns, selected = _by_database_setup(catalog)
    opened = len(listed)
    keep = by_database.keeps_plans(opened * catalog.tables_per_database)
    units = [by_database.Unit(spec["src_connection"], name, True) for name in listed]
    reads = [0.0]
    members: set = set()
    connection = lakebase_fake.RecordingConnection(answers=_member_answers(members))

    def rows_of(batch: List[by_database.Unit]):
        started = time.perf_counter()
        found = [row for unit in batch for row in _database_rows(server, spec, unit.database)]
        reads[0] += time.perf_counter() - started
        connection.record(q.database_rows_chunk_sql(SOURCE_KIND), len(found))
        return by_database.rows_by_unit(batch, found)

    with lakebase_fake.no_connections():
        started, reads_before = time.perf_counter(), reads[0]
        errors, tables, groups, counted = [], 0, set(), []
        _record_pages(connection, opened)
        for batch in by_database.read_batches(units):
            for unit, rows in rows_of(batch):
                if not rows:
                    continue
                plan = by_database.plan_database(SCALE_JOB, CREATE_SETTINGS, rows, columns, groups_of, set(), set())
                errors += plan["errors"]
                tables += len(plan["lines"])
                groups.update(plan["groups"])
                counted.append(by_database.Checked(unit, len(rows), plan if keep else None))
        check_s = time.perf_counter() - started - (reads[0] - reads_before)

        state = {"stage": "writing", "databases_total": opened, "databases_written": 0, "tables_written": 0}
        started, reads_before = time.perf_counter(), reads[0]
        if not errors:
            schedule_id = _open_job(connection, selected)
            job = app.ChunkJob(sid=schedule_id, name=SCALE_JOB, user=SCALE_USER, batch=SCALE_BATCH,
                               cadence_minutes=CADENCE_MINUTES, timezone=TIMEZONE, offsets={}, task_id=TASK_ID,
                               stages=by_database.Stages())
            for chunk in by_database.transactions_of(counted):
                plans = [checked.plan for checked in chunk] if keep else [
                    by_database.plan_database(SCALE_JOB, CREATE_SETTINGS, rows, columns, groups_of, set(), set())
                    for _, rows in rows_of([checked.unit for checked in chunk]) if rows]
                if plans:
                    with connection.transaction() as cur:
                        app._write_chunk(cur, plans, job, state)
            with connection.transaction() as cur:
                app._task_written(cur, TASK_ID, {"id": schedule_id}, schedule_id, tables)
        write_s = time.perf_counter() - started - (reads[0] - reads_before)
    return ByDatabaseRun(databases=state["databases_written"], tables=tables, groups=sorted(groups),
                         errors=list(errors), check_s=check_s, write_s=write_s, read_s=reads[0], connection=connection)


def upload_by_reference(catalog: server_catalog.ServerCatalog, limit: Optional[int] = None) -> UploadRun:
    """Upload the discovered tables of a server as a CSV, as upload_tables does: read it, then write the header and one
    line per table, q.CONTROL_BATCH lines to a statement. Runs under no_connections()."""
    with lakebase_fake.no_connections():
        rows = discovered_rows(catalog, limit=limit)
        text = nj.csv_of(_upload_columns(), rows)
        return _upload_text(text, lines_hint=len(rows))


def upload_text(text: str) -> UploadRun:
    """read_upload and the upload loop on a CSV text, as upload_tables runs them. Runs under no_connections()."""
    with lakebase_fake.no_connections():
        return _upload_text(text, lines_hint=None)


MONITORING_PATHS = ("/api/overview", "/api/collections", "/api/errors", "/api/ingestion-jobs", "/api/ingestion-jobs/1",
                    "/api/ingestion-jobs/1/live", "/api/runs", "/api/tables", "/api/changes", "/api/schedules",
                    "/api/timeline", "/api/discoveries")


def monitoring_shapes(paths: Tuple[str, ...] = MONITORING_PATHS) -> List[dict]:
    """The Lakebase statements each monitoring page issues: the app's own routes through TestClient, with the database
    stubbed to record and answer with no rows. This gives the shape of each page (which statements, how many); the rows
    a statement returns at scale are not in it. Runs under no_connections()."""
    from unittest import mock

    from fastapi.testclient import TestClient

    client = TestClient(app.app, headers={"X-Forwarded-Email": SCALE_USER})
    shapes = []
    with lakebase_fake.no_connections():
        for path in paths:
            recorded = lakebase_fake.RecordingConnection()
            with mock.patch.object(app.db, "query", lambda sql, params=(): (recorded.record(sql, 0), [])[1]), \
                    mock.patch.object(app.db, "execute", lambda sql, params=(): recorded.record(sql, 0)):
                response = client.get(path)
            shapes.append({"path": path, "status": response.status_code, "statements": recorded.total_statements(),
                           "kinds": dict(recorded.statements)})
    return shapes


def _upload_text(text: str, lines_hint: Optional[int]) -> UploadRun:
    started = time.perf_counter()
    read = nj.read_upload(text, sim.connection_spec()["src_connection"])
    read_s = time.perf_counter() - started
    if read["errors"]:
        return UploadRun(lines=lines_hint or 0, errors=list(read["errors"]), csv_bytes=len(text.encode()),
                         read_s=read_s, write_s=0.0, connection=None)
    upload_id, lines = "scale-upload", read["lines"]
    ok = sum(line["status"] == "ok" for line in lines)
    connection = lakebase_fake.RecordingConnection()
    started = time.perf_counter()
    with connection.transaction() as cur:
        cur.execute(q.insert_upload_sql(), (None,) * 7)
        for at in range(0, len(lines), q.CONTROL_BATCH):
            chunk = lines[at:at + q.CONTROL_BATCH]
            cur.execute(q.insert_upload_lines_sql(len(chunk)), tuple(
                v for line in chunk for v in (upload_id, line["line"], line["server"], line["src_database"],
                                              line["src_schema"], line["src_table"], line["load_type"],
                                              json.dumps(line["fields"]), line["status"], line["errors"])))
    write_s = time.perf_counter() - started
    return UploadRun(lines=len(lines), errors=[], csv_bytes=len(text.encode()), read_s=read_s, write_s=write_s,
                     connection=connection)


def _upload_columns() -> List[str]:
    """The discovery columns an upload takes: the file reads through the connection chosen before it, so not the
    server's own columns (new_job.UPLOAD_REFUSED_COLUMNS)."""
    return [c for c in nj.DISCOVERY_COLUMNS if c not in nj.UPLOAD_REFUSED_COLUMNS]


def _repeated_csv(line_count: int, columns: Optional[List[str]] = None) -> str:
    """A CSV of line_count copies of one table: enough lines to reach a limit, and cheap to build."""
    sample = _discovered_fields(sim.connection_spec(), "lfc_eng_00000", {"src_schema": "dbo", "src_table": "t_00000"})
    return nj.csv_of(columns or list(nj.DISCOVERY_COLUMNS), [sample] * line_count)


def create_refusal(line_count: int) -> Tuple[List[str], float]:
    """nj.plan on a CSV of line_count lines: its refusal, and how long the planning took to give it."""
    text = _repeated_csv(line_count)
    started = time.perf_counter()
    plan = nj.plan(SCALE_JOB, text, CREATE_SETTINGS, taken_groups=set())
    return list(plan["errors"]), time.perf_counter() - started


def upload_refusal(line_count: int) -> Tuple[List[str], float]:
    """read_upload on a CSV of line_count lines: its refusal, and how long reading took to give it."""
    text = _repeated_csv(line_count, _upload_columns())
    started = time.perf_counter()
    read = nj.read_upload(text, sim.connection_spec()["src_connection"])
    return list(read["errors"]), time.perf_counter() - started


def sample_line_bytes(line_count: int = 1000) -> float:
    """The size of one CSV line of a discovered table in bytes, header excluded: the body of an upload is this times its
    lines (JSON escaping adds a little more, which is left out)."""
    text = _repeated_csv(line_count)
    header_bytes = text.index("\n") + 1
    return (len(text.encode()) - header_bytes) / line_count
