"""Re-discovery: a daily look for the new databases and tables an ingestion job's selection would take.

A job made from a discovery keeps what it was chosen from (job_selection): a whole server with its filter, whole
databases, or some tables. When the job's schedule has a re-discovery hour (options.rediscover_hour, UTC), the
discovery runs again over the same servers each day, and the rules add what they allow:

- a whole server (or every database matching its filter) adds every new database matching the filter, with all of
  its tables;
- a whole database adds its new tables;
- specific tables add nothing.

A table the source no longer has is kept, and reported as vanished. This module holds the pure parts: when a job is
due, the discovery specs that read each server again, the rules, and the record the job page shows. main.py runs them
and writes the tables through the same path as Add tables."""
import re
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

SETTING = "rediscover_hour"
RECORD = "rediscovery"
# A running re-discovery writes its heartbeat every BEAT_S seconds. One whose heartbeat is older than STALE_S has no
# thread left (the app restarted while it ran), so the job may run again and the page says it failed.
BEAT_S = 15
STALE_S = 120
RESTARTED = "the app restarted; run it again"
LISTING_KEYS = ("pattern", "azure_credential", "azure_resource_id")
REPORT_LIMIT = 20
# The phases a running re-discovery records: reading a server again (its discovery run goes), then writing the tables.
DISCOVERING, ADDING = "discovering", "adding"


def hour_of(options: Optional[dict]) -> Optional[int]:
    """The UTC hour a job re-discovers at (0 to 23), or None when re-discovery is off."""
    hour = (options or {}).get(SETTING)
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        return None
    return hour


def running_record(now: datetime) -> dict:
    """The record a re-discovery writes when it starts (its claim on the schedule)."""
    stamp = now.astimezone(timezone.utc).isoformat()
    return {"status": "running", "started_at": stamp, "heartbeat_at": stamp}


def abandoned(record: Optional[dict], now: datetime) -> bool:
    """Whether a running re-discovery has no thread left: its heartbeat (its start, before the first beat) is older
    than STALE_S."""
    beat = _as_utc((record or {}).get("heartbeat_at") or (record or {}).get("started_at"))
    return beat is not None and (now.astimezone(timezone.utc) - beat).total_seconds() > STALE_S


def is_due(options: Optional[dict], now: datetime, paused: bool = False) -> bool:
    """Whether a job should re-discover now: its hour has come (UTC), it is not paused, it is not running, and it has
    not re-discovered yet today (UTC). A job whose hour came before it was set runs on the first tick after that."""
    hour = hour_of(options)
    now = now.astimezone(timezone.utc)
    if hour is None or paused or now.hour < hour:
        return False
    record = (options or {}).get(RECORD) or {}
    if record.get("status") == "running" and not abandoned(record, now):
        return False
    started = _as_utc(record.get("started_at"))
    return started is None or started.date() < now.date()


def view(options: Optional[dict], now: datetime, discovery_outcome: Optional[dict] = None) -> dict:
    """What the job page shows: the hour (or None when off), the last re-discovery's record (or None), and its progress."""
    last = (options or {}).get(RECORD) or None
    return {"hour": hour_of(options), "last": last, "progress": progress(last, now, discovery_outcome)}


def progress(last: Optional[dict], now: datetime, discovery_outcome: Optional[dict] = None) -> Optional[dict]:
    """Where the last re-discovery stands, for the job page. ``pending`` while its discovery run goes (the outcome is
    the run's, from the run poller: its page, or a timeout once it is past its wait) or while the record has not moved
    on yet; ``adding`` while the tables are written; ``done`` with what was found and added; ``failed`` with the reason
    (a deferred run included); ``timeout`` when a run has gone on past the stale limit with no end recorded."""
    if not last:
        return None
    status = last.get("status")
    if status == "done":
        return {"state": "done", "found": last.get("found", 0), "added": last.get("added", 0), "note": last.get("note")}
    if status in ("failed", "deferred"):
        return {"state": "failed", "reason": last.get("error") or "no reason was recorded"}
    if status == "running" and abandoned(last, now):
        return {"state": "failed", "reason": RESTARTED}
    if last.get("phase") == ADDING:
        return {"state": ADDING, "server": None}
    outcome = discovery_outcome or {}
    if outcome.get("state") == "timeout":
        return {"state": "timeout", "reason": outcome.get("reason"), "url": outcome.get("url"), "server": last.get("server")}
    return {"state": "pending", "url": outcome.get("url"), "server": last.get("server")}


def server_key(server: Optional[str]) -> str:
    """A server as the discovery and the job keep it: lower case, without a port."""
    return re.sub(r":\d+$", "", str(server or "").strip().lower())


def control_server(row: dict) -> str:
    """The server key of an ingestion job's control row (its UC connection, or its host)."""
    return server_key(row.get("src_connection") or row.get("src_host"))


def specs(parts: List[dict], sources: List[dict]) -> Tuple[List[Tuple[str, dict]], List[str]]:
    """The discovery specs that read again each server the discovery parts name: a (server, spec) pair per spec, and
    the reasons a server can't be read again. A server's databases read through one spec, except that a host whose
    databases take different secrets (no template recorded) reads each secret's databases apart. ``sources`` are the
    discovery rows the parts were made from: one per database, with the listing the discovery recorded."""
    out: List[Tuple[str, dict]] = []
    errors: List[str] = []
    for server in sorted({server_key(p["server"]) for p in parts if p.get("source_kind") == "discovery"}):
        whole = any(p["scope"] == "server" and server_key(p["server"]) == server for p in parts)
        names = sorted({p["src_database"] for p in parts
                        if p["scope"] == "database" and server_key(p["server"]) == server}, key=str.lower)
        if not whole and not names:
            continue
        mine = [s for s in sources if server_key(s["server"]) == server]
        if not mine:
            errors.append(f"{server}: its discovery is no longer kept, so it can't be read again")
            continue
        newest = max(mine, key=lambda s: str(s.get("finished_at") or ""))
        listing = newest.get("listing")
        if whole and listing is None:
            errors.append(f"{server}: it was discovered before re-discovery kept how it lists databases: discover it "
                          "again from Add tables")
            continue
        listing = listing or {}
        base = {"src_type": newest.get("src_type") or "sqlserver", "kind": "tables", "requested_by": "re-discovery"}
        if newest.get("src_connection"):
            groups = [(None, names)]
        else:
            secret_of = {str(s["src_database"]).lower(): s.get("secret_key") for s in mine if s.get("secret_key")}
            distinct = sorted(set(secret_of.values()))
            if listing.get("secret_key_template"):
                groups = [(None, names)]
            elif whole:
                if len(distinct) != 1:
                    errors.append(f"{server}: its databases take different secrets and no template was recorded: "
                                  "discover it again from Add tables")
                    continue
                groups = [(distinct[0], names)]
            else:
                by_secret: dict = {}
                missing = [n for n in names if str(n).lower() not in secret_of]
                if missing:
                    errors.append(f"{server}: no secret is recorded for {missing[0]}: discover it again "
                                  "from Add tables")
                    continue
                for name in names:
                    by_secret.setdefault(secret_of[str(name).lower()], []).append(name)
                groups = list(by_secret.items())
        for secret, group_names in groups:
            spec = dict(base)
            if listing.get("jdbc_connection"):
                spec["jdbc_connection"] = listing["jdbc_connection"]
            if newest.get("src_connection"):
                spec["src_connection"] = newest["src_connection"]
            else:
                spec["src_host"] = newest["src_host"]
                spec["src_port"] = newest.get("src_port")
                if listing.get("secret_key_template"):
                    spec["secret_key_template"] = listing["secret_key_template"]
                else:
                    spec["secret_key"] = secret
            if whole:
                spec["databases"] = "*"
                spec.update({key: listing[key] for key in LISTING_KEYS if listing.get(key)})
            else:
                spec["databases"] = list(group_names)
            out.append((server, spec))
    return out, errors


def table_key(server: str, database: str, schema: str, table: str) -> tuple:
    return tuple(str(part or "").lower() for part in (server_key(server), database, schema, table))


def wanted(parts: List[dict], found: List[dict], statuses: List[dict], job_rows: List[dict],
           excluded: Set[tuple] = frozenset()) -> dict:
    """What the rules add to a job, given what the re-discovery found now.

    parts: the job's selection parts (scope, server, src_database, filter). found: the tables the discovery found
    (server, src_database, src_schema, src_table, fields). statuses: each database the discovery read (server,
    src_database, status 'ok' or 'failed', error). job_rows: the job's control rows now. excluded: the tables the job
    removed (table keys): they are not added again, and a database whose only new tables are excluded is not new.

    Returns {"add": the found rows to add (sorted by table), "new_databases": "server/database" names that a whole
    server part adds, "vanished": tables the job has that the source no longer has (kept, reported),
    "errors": the databases the discovery could not open, of the parts' servers and databases}. A database that
    failed contributes nothing and adds nothing; the others are unaffected."""
    have_tables = {table_key(control_server(r), r["src_database"], r["src_schema"], r["src_table"]) for r in job_rows}
    have_databases = {(control_server(r), str(r["src_database"]).lower()) for r in job_rows}
    found_tables = {table_key(r["server"], r["src_database"], r["src_schema"], r["src_table"]) for r in found}
    listed = {(server_key(s["server"]), str(s["src_database"]).lower()) for s in statuses if s["status"] == "ok"}
    whole_servers = {server_key(p["server"]) for p in parts if p["scope"] == "server"}
    database_parts = {(server_key(p["server"]), str(p["src_database"]).lower())
                      for p in parts if p["scope"] == "database"}
    add, new_databases = {}, set()
    for part in parts:
        server = server_key(part["server"])
        if part["scope"] == "server":
            needle = str(part.get("filter") or "").lower()
            for row in found:
                database = str(row["src_database"])
                if server_key(row["server"]) != server or needle not in database.lower():
                    continue
                key = table_key(row["server"], database, row["src_schema"], row["src_table"])
                if (server, database.lower()) in have_databases or key in excluded:
                    continue
                new_databases.add(f"{server}/{database}")
                if key not in have_tables:
                    add.setdefault(key, row)
        elif part["scope"] == "database":
            for row in found:
                row_database = (server_key(row["server"]), str(row["src_database"]).lower())
                if row_database != (server, str(part["src_database"]).lower()):
                    continue
                key = table_key(row["server"], row["src_database"], row["src_schema"], row["src_table"])
                if key not in have_tables and key not in excluded:
                    add.setdefault(key, row)
    vanished = []
    for row in job_rows:
        server, database = control_server(row), str(row["src_database"]).lower()
        in_scope = server in whole_servers or (server, database) in database_parts
        if not in_scope or (server, database) not in listed:
            continue
        if table_key(server, row["src_database"], row["src_schema"], row["src_table"]) not in found_tables:
            vanished.append(f"{server}/{row['src_database']}.{row['src_schema']}.{row['src_table']}")
    errors = [{"database": f"{s['server']}/{s['src_database']}", "error": s.get("error") or "could not be read"}
              for s in statuses if s["status"] == "failed"
              and (server_key(s["server"]) in whole_servers
                   or (server_key(s["server"]), str(s["src_database"]).lower()) in database_parts)]
    return {"add": [add[key] for key in sorted(add)], "new_databases": sorted(new_databases),
            "vanished": sorted(set(vanished)), "errors": errors}


def _as_utc(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
