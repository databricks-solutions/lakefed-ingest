"""Deleting an operations-app job's tables (run by delete_job.ipynb, job lakefed_ingest_delete_job).

The app has no DELETE privilege on the Lakebase tables, so a deleted job's groups' rows can only be removed here. The
app marks a job deleted and keeps it for the retention period (the bundle variable deleted_job_retention_days), so a
Restore can bring it back. This job runs as the deployer (the state database's owner) and purges it:

- **Which groups:** the deleted job's members that no other live job holds (a group is in one job, but a removed
  member could have joined another job since).
- **When:** a job's delete run (its id given) purges it at once, from the app's Delete now. The daily run purges
  every deleted job whose retention has run out (purge_expired), each in its own transaction.
- **Finished:** the purge sets the job's options.delete_finished in the same transaction as the rows, so the app
  lists it as purged and refuses a Restore.
- **What goes, in one transaction under each group's run lock:** the groups' control rows and what is keyed by them
  (checkpoint, reseed_queue) or by the group (database_check, check_work, and the monitoring rollups
  rollup_database and rollup_group). A run of any of the groups refuses the
  lot: nothing is deleted while one is loading.
- **What it was made from:** the job's job_selection rows (the servers, databases and tables it was chosen from, for
  a later re-discovery), in the same transaction, so a deleted job never takes new tables. Skipped on a state
  database still below schema version 9, which has no such table.
- **What stays:** run history (ingest_run, ingest_event) and the change log (control_change), so the job's runs and
  changes can still be read after it is gone; their rows keep the group and control id of rows that no longer exist.
- **Target tables, when asked:** after the commit, the tables only these groups write; one another job's rows also
  write is kept and named. A schema is never dropped.
- **One table (control_ids, with schedule_id):** a live job's table removed in the app. Its control row and its
  checkpoint and reseed_queue rows go, in one transaction under its group's run lock; the rollups of its database
  and group are recounted; its target table and the job's other tables stay (delete_table_rows).
- **Drafts (purge_drafts):** the app reads a choice's tables as they are chosen, into discovery rows marked draft,
  before the job exists. A draft discarded (the page was cancelled or left) or that nobody has asked about for
  draft_ttl_s is purged: its discovery and discovered_table rows. A draft a Create or an Add took has no mark, so it
  is never purged. The app starts this mode at most hourly, and only when there is a draft to purge.

Pure (no Spark, no Lakebase): the notebook passes a cursor and runs the drops; tested with fakes.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ct_engine import sink_fqn
from state_store import (ROLLUP_DATABASE_TABLE, ROLLUP_GROUP_TABLE, ROLLUP_LOCK_NAMESPACE, collection_lock_key,
                         delete_rollups_sql, rollup_database_rewrite_sql, rollup_group_lock_sql,
                         rollup_group_rewrite_sql)

SCHEMA = "lakefed_ingest"


def schedule_sql() -> str:
    """The job, deleted or not, locked for this transaction: a Restore of it waits for the purge, then finds it purged.
    Param: schedule id."""
    return f"select id, name, deleted_at from {SCHEMA}.schedule where id = %s for update"


def expired_jobs_sql() -> str:
    """The deleted jobs whose retention has run out and whose delete hasn't finished, oldest first: their id, name and
    whether they asked for their target tables to go. Param: retention days."""
    return (f"select id, name, coalesce((options ->> 'delete_drop_targets')::boolean, false) as drop_targets "
            f"from {SCHEMA}.schedule where deleted_at is not null and deleted_at <= now() - make_interval(days => %s) "
            f"and not coalesce((options ->> 'delete_finished')::boolean, false) order by deleted_at, id")


def expired_check_sql() -> str:
    """One deleted job's row when its retention has run out and its delete hasn't finished, locked for this
    transaction (a Restore waits for it), with whether it asked for its target tables to go. Params: schedule id,
    retention days."""
    return (f"select coalesce((options ->> 'delete_drop_targets')::boolean, false) from {SCHEMA}.schedule "
            f"where id = %s and deleted_at is not null and deleted_at <= now() - make_interval(days => %s) "
            f"and not coalesce((options ->> 'delete_finished')::boolean, false) for update")


def finished_sql() -> str:
    """Mark a deleted job's delete finished: its groups' rows are gone, so the app lists it as purged. Param: schedule
    id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), "
            f"'{{delete_finished}}', 'true'::jsonb) where id = %s")


def groups_sql() -> str:
    """The job's groups no other live job holds: every member it had, removed or not, that isn't an active member of
    another job. Params: schedule id, schedule id."""
    return (f"select distinct m.task_collection from {SCHEMA}.schedule_member m where m.schedule_id = %s "
            f"and not exists (select 1 from {SCHEMA}.schedule_member o join {SCHEMA}.schedule s on s.id = o.schedule_id "
            f"where o.task_collection = m.task_collection and o.removed_at is null and s.deleted_at is null "
            f"and o.schedule_id <> %s) order by 1")


def lock_sql() -> str:
    """A group's run lock for this transaction, on the key a run holds (state_store.collection_lock_key); false: a
    run of it is loading. Param: lock key."""
    return "select pg_try_advisory_xact_lock(hashtextextended(%s, 0))"


def targets_sql() -> str:
    """Each target table the groups' rows write, and whether rows of any other group write it too (names compared
    without case, as Unity Catalog and control_sink_client_uq do). Params: groups, groups."""
    return (f"with mine as (select distinct sink_catalog, sink_schema, sink_table from {SCHEMA}.control "
            f"where task_collection = any(%s)), "
            f"theirs as (select distinct lower(sink_catalog) c, lower(sink_schema) s, lower(sink_table) t "
            f"from {SCHEMA}.control where not (task_collection = any(%s))) "
            f"select m.sink_catalog, m.sink_schema, m.sink_table, "
            f"exists (select 1 from theirs where c = lower(m.sink_catalog) and s = lower(m.sink_schema) "
            f"and t = lower(m.sink_table)) as shared from mine m order by 1, 2, 3")


# Keyed by the groups' control rows first, then by the group, then the rows themselves: no foreign keys hold them
# together, so the order only keeps a failure part way from leaving checkpoints of rows already gone (the
# transaction undoes it all anyway). Each takes the groups as its one parameter.
DELETES = (
    ("reseed_queue", f"delete from {SCHEMA}.reseed_queue where control_id in "
                     f"(select id from {SCHEMA}.control where task_collection = any(%s))"),
    ("checkpoint", f"delete from {SCHEMA}.checkpoint where control_id in "
                   f"(select id from {SCHEMA}.control where task_collection = any(%s))"),
    ("database_check", f"delete from {SCHEMA}.database_check where task_collection = any(%s)"),
    ("check_work", f"delete from {SCHEMA}.check_work where task_collection = any(%s)"),
    ("rollup_database", delete_rollups_sql(ROLLUP_DATABASE_TABLE)),
    ("rollup_group", delete_rollups_sql(ROLLUP_GROUP_TABLE)),
    ("control", f"delete from {SCHEMA}.control where task_collection = any(%s)"),
)


def selection_table_sql() -> str:
    """Whether the state database has job_selection (schema version 9 and later)."""
    return f"select to_regclass('{SCHEMA}.job_selection') is not null"


def delete_selection_sql() -> str:
    """The job's job_selection rows. Param: schedule id."""
    return f"delete from {SCHEMA}.job_selection where job_id = %s"


def _forget_selection(cur, schedule_id: int) -> dict:
    """Delete what the job was made from, where the state database keeps it: {"job_selection": rows} when any went."""
    cur.execute(selection_table_sql())
    if not cur.fetchone()[0]:
        return {}
    cur.execute(delete_selection_sql(), (int(schedule_id),))
    return {"job_selection": cur.rowcount} if cur.rowcount else {}


def drop_sql(table: str) -> str:
    """Drops one target table, named as plan_targets names it. Never a schema."""
    return f"DROP TABLE IF EXISTS {table}"


def plan_targets(rows: List[dict]) -> Dict[str, List[str]]:
    """{"drop": tables only these groups write, "kept": tables other groups' rows write too}, as the quoted names
    the ingestion job writes (ct_engine.sink_fqn), each once."""
    drop = list(dict.fromkeys(sink_fqn(r) for r in rows if not r["shared"]))
    kept = list(dict.fromkeys(sink_fqn(r) for r in rows if r["shared"]))
    return {"drop": drop, "kept": kept}


class RunGoing(RuntimeError):
    """A run of one of the job's groups is loading: nothing was deleted."""


def _rows(cur) -> List[dict]:
    columns = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def delete_rows(cur, schedule_id: int) -> dict:
    """Inside the caller's transaction: check the job is deleted, take each group's run lock, read what its tables
    write, then delete the groups' rows. Returns {"job", "groups", "deleted": {table: rows}, "targets": plan}.
    Raises ValueError for a job that isn't deleted (or doesn't exist) and RunGoing while a group is loading."""
    cur.execute(schedule_sql(), (int(schedule_id),))
    found = _rows(cur)
    if not found:
        raise ValueError(f"ingestion job {schedule_id} doesn't exist")
    job = found[0]
    if job["deleted_at"] is None:
        raise ValueError(f"ingestion job {job['name']} ({schedule_id}) isn't deleted: delete it in the app first")
    cur.execute(groups_sql(), (int(schedule_id), int(schedule_id)))
    groups = [r["task_collection"] for r in _rows(cur)]
    deleted, targets = {}, {"drop": [], "kept": []}
    if groups:
        for group in groups:
            cur.execute(lock_sql(), (collection_lock_key(group),))
            if not cur.fetchone()[0]:
                raise RunGoing(f"a run of group {group} is loading: nothing was deleted; "
                               "run the delete again once it ends")
        cur.execute(targets_sql(), (groups, groups))
        targets = plan_targets(_rows(cur))
        for table, sql in DELETES:
            cur.execute(sql, (groups,))
            deleted[table] = cur.rowcount
    deleted.update(_forget_selection(cur, schedule_id))
    cur.execute(finished_sql(), (int(schedule_id),))
    return {"job": job["name"], "groups": groups, "deleted": deleted, "targets": targets}


def job_groups_sql() -> str:
    """The groups of a live job that no other live job holds: the only groups a table can leave without changing
    another job's tables. Params: schedule id, schedule id."""
    return (f"select distinct m.task_collection from {SCHEMA}.schedule_member m where m.schedule_id = %s "
            f"and m.removed_at is null and not exists (select 1 from {SCHEMA}.schedule_member o "
            f"join {SCHEMA}.schedule s on s.id = o.schedule_id where o.task_collection = m.task_collection "
            f"and o.removed_at is null and s.deleted_at is null and o.schedule_id <> %s) order by 1")


def table_rows_sql() -> str:
    """The control rows named by id, with the group and database each belongs to and its target. Param: control ids."""
    return (f"select id, task_collection, coalesce(src_database, '') as src_database, sink_catalog, sink_schema, "
            f"sink_table from {SCHEMA}.control where id = any(%s) order by id")


# A removed table's own state, keyed by its control row. The target table, database_check (a whole database's stamp,
# still true for the tables that stay) and check_work (keyed by database) are not touched.
TABLE_DELETES = (
    ("reseed_queue", f"delete from {SCHEMA}.reseed_queue where control_id = any(%s)"),
    ("checkpoint", f"delete from {SCHEMA}.checkpoint where control_id = any(%s)"),
    ("control", f"delete from {SCHEMA}.control where id = any(%s)"),
)


def delete_table_rows(cur, schedule_id: int, control_ids) -> dict:
    """Inside the caller's transaction: remove some tables from a live job. Their control rows and their own state go
    (checkpoint, reseed_queue), the rollups of their databases and groups are recounted, and the target tables are
    kept. The job, its other tables and run history stay. Takes the same run locks as a job's Delete: a run of a
    table's group refuses the lot. Returns {"job", "groups", "control_ids", "missing", "deleted", "databases",
    "targets"}. Raises ValueError for a deleted job, a row outside the job's own groups, or no ids; RunGoing while a
    group is loading."""
    ids = sorted({int(i) for i in control_ids})
    if not ids:
        raise ValueError("no control ids to remove")
    cur.execute(schedule_sql(), (int(schedule_id),))
    found = _rows(cur)
    if not found:
        raise ValueError(f"ingestion job {schedule_id} doesn't exist")
    job = found[0]
    if job["deleted_at"] is not None:
        raise ValueError(f"ingestion job {job['name']} ({schedule_id}) is deleted: its delete job removes its tables")
    cur.execute(job_groups_sql(), (int(schedule_id), int(schedule_id)))
    own = {r["task_collection"] for r in _rows(cur)}
    cur.execute(table_rows_sql(), (ids,))
    rows = _rows(cur)
    stray = sorted({r["task_collection"] for r in rows} - own)
    if stray:
        raise ValueError(f"group(s) {', '.join(stray)} aren't held by ingestion job {job['name']} alone: remove the "
                         "table in the other job, or delete that group's job")
    missing = sorted(set(ids) - {r["id"] for r in rows})
    if not rows:
        return {"job": job["name"], "groups": [], "control_ids": [], "missing": missing,
                "deleted": {table: 0 for table, _ in TABLE_DELETES}, "databases": 0,
                "targets": {"drop": [], "kept": []}}
    groups = sorted({r["task_collection"] for r in rows})
    for group in groups:
        cur.execute(lock_sql(), (collection_lock_key(group),))
        if not cur.fetchone()[0]:
            raise RunGoing(f"a run of group {group} is loading: nothing was removed; remove the table again once it ends")
    live = [r["id"] for r in rows]
    cur.execute(rollup_group_lock_sql(), (ROLLUP_LOCK_NAMESPACE, groups))
    deleted = {}
    for table, sql in TABLE_DELETES:
        cur.execute(sql, (live,))
        deleted[table] = cur.rowcount
    databases = sorted({(r["task_collection"], r["src_database"]) for r in rows})
    cur.execute(rollup_database_rewrite_sql(), ([g for g, _ in databases], [d for _, d in databases]))
    cur.execute(rollup_group_rewrite_sql(), (groups,))
    kept = list(dict.fromkeys(sink_fqn(r) for r in rows))
    return {"job": job["name"], "groups": groups, "control_ids": live, "missing": missing, "deleted": deleted,
            "databases": len(databases), "targets": {"drop": [], "kept": kept}}


def expired_jobs(cur, retention_days: int) -> List[dict]:
    """Inside the caller's transaction: the deleted jobs whose retention has run out, as {"id", "name",
    "drop_targets"} rows (see expired_jobs_sql)."""
    cur.execute(expired_jobs_sql(), (int(retention_days),))
    return _rows(cur)


def delete_expired(cur, schedule_id: int, retention_days: int) -> Optional[dict]:
    """Inside the caller's transaction: delete_rows for one deleted job whose retention has run out, with its
    ``drop_targets`` choice added. Returns None when it has no longer (a Restore, or its purge, came first): nothing
    is deleted then. Raises RunGoing as delete_rows does."""
    cur.execute(expired_check_sql(), (int(schedule_id), int(retention_days)))
    found = cur.fetchall()
    if not found:
        return None
    result = delete_rows(cur, schedule_id)
    result["drop_targets"] = bool(found[0][0])
    return result


def purge_expired(transaction, retention_days: int, drop_tables) -> dict:
    """Purge every deleted job whose retention has run out, each in its own transaction, so one job's run going
    doesn't hold up the others (a job whose group is loading waits for the next daily run). ``transaction()`` opens
    one transaction and yields its cursor; ``drop_tables(tables)`` drops them after the commit and returns
    {table: error} for the ones it couldn't. A job's target tables are dropped only if it asked for them.
    Returns {"listed", "purged": [summary per job], "waiting": [{"job", "reason"}], "failed": {table: error}}."""
    with transaction() as cur:
        listed = expired_jobs(cur, retention_days)
    purged, waiting, failed = [], [], {}
    for job in listed:
        try:
            with transaction() as cur:
                result = delete_expired(cur, job["id"], retention_days)
        except RunGoing as error:
            waiting.append({"job": job["name"], "reason": str(error)})
            continue
        if result is None:
            continue
        to_drop = result["targets"]["drop"] if result["drop_targets"] else []
        drop_failed = drop_tables(to_drop) if to_drop else {}
        failed.update(drop_failed)
        purged.append(summary(result, [t for t in to_drop if t not in drop_failed], drop_failed))
    return {"listed": len(listed), "purged": purged, "waiting": waiting, "failed": failed}


DRAFT_TTL_S = 2 * 3600


def purgeable_drafts_sql() -> str:
    """The drafts to purge: discarded, or not asked about (touched_at on their first row, else when they started) for
    the time limit. Param: seconds."""
    return (f"select discovery_id from {SCHEMA}.discovery where listing ? 'draft' group by discovery_id "
            f"having bool_or(listing ? 'discarded') or coalesce(max((listing ->> 'touched_at')::timestamptz), "
            f"max(started_at)) < now() - make_interval(secs => %s)")


def purge_discovery_sql() -> str:
    """Delete these drafts' discovery rows, those still marked draft. Param: discovery ids (text[])."""
    return (f"delete from {SCHEMA}.discovery where discovery_id = any(%s) and listing ? 'draft' "
            f"returning discovery_id")


def purge_tables_sql() -> str:
    """Delete the tables read for these drafts. Param: discovery ids (text[])."""
    return f"delete from {SCHEMA}.discovered_table where discovery_id = any(%s)"


def purge_drafts(cur, ttl_s: int = DRAFT_TTL_S) -> dict:
    """Inside the caller's transaction: purge the drafts discarded or not asked about for ``ttl_s``. Their discovery
    rows go first, each only while still marked draft (a Create taking it meanwhile holds its rows until it commits,
    and takes the mark off), then the tables of the drafts whose rows went. Returns the counts."""
    cur.execute(purgeable_drafts_sql(), (int(ttl_s),))
    ids = [row[0] for row in cur.fetchall()]
    if not ids:
        return {"drafts": 0, "databases": 0, "tables": 0}
    cur.execute(purge_discovery_sql(), (ids,))
    gone = sorted({row[0] for row in cur.fetchall()})
    databases = cur.rowcount
    tables = 0
    if gone:
        cur.execute(purge_tables_sql(), (gone,))
        tables = cur.rowcount
    return {"drafts": len(gone), "databases": databases, "tables": tables}


# Names a summary or a failure lists; the rest are counted, so a job of many thousands of tables stays readable.
LISTED = 20


def summary(result: dict, dropped: List[str], failed: Optional[Dict[str, str]] = None) -> dict:
    """What the run did, for its exit value: counts, with the kept and failed target tables named up to LISTED."""
    failed = failed or {}
    return {"job": result["job"], "groups": len(result["groups"]), "tables": result["deleted"].get("control", 0),
            "deleted": result["deleted"], "dropped": len(dropped), "kept": len(result["targets"]["kept"]),
            "kept_names": result["targets"]["kept"][:LISTED], "drop_failed": len(failed),
            "drop_failed_names": dict(list(failed.items())[:LISTED])}


def failure_message(failed: Dict[str, str]) -> str:
    """Why the run fails after its rows are deleted: the target tables it couldn't drop, which a second run won't
    find (the rows that listed them are gone), so they are named to be dropped by hand."""
    named = "; ".join(f"{table}: {error}" for table, error in list(failed.items())[:LISTED])
    more = f"; and {len(failed) - LISTED} more (the run's output lists them)" if len(failed) > LISTED else ""
    return f"the job's tables are deleted, but {len(failed)} target table(s) could not be dropped (drop them by hand): {named}{more}"
