"""Sums of the per-database rows (queries.database_states_sql) into what the monitoring views show: per
ingestion group, per server and per UC connection. Pure, so the views can share one scan of the control
table and still be tested without a database.

A database is its server and its name: two servers' databases of the same name are two databases. A
database counts once it has an enabled table."""
from typing import Dict, Iterable, List, Optional

# Tables by state: loaded (current or late), waiting for a first load, failing. A late database (one with a late
# table) is counted per database, not per table: the top-level views say so, and the database page counts tables.
STATES = ("state_loaded", "state_first_load", "state_failing")


def _count(row: dict, key: str) -> int:
    return int(row.get(key) or 0)


def from_scan(row: dict) -> dict:
    """A database_states row (a scan of the control table, for a group with no rollup row yet) in the shape of a
    rollup row: its loaded tables are the current and the late ones, and it is late when any of its tables is."""
    late = _count(row, "state_late")
    return {**{k: v for k, v in row.items() if k not in ("state_current", "state_late")},
            "state_loaded": _count(row, "state_current") + late, "late_database": late > 0}


def _later(a, b):
    return b if a is None or (b is not None and b > a) else a


def _earlier(a, b):
    return b if a is None or (b is not None and b < a) else a


def groups(states: Iterable[dict]) -> Dict[str, dict]:
    """Per ingestion group: enabled tables, tables without a freshness target, tables by state, its
    databases and servers (with an enabled table), its first server by name, and its newest success.
    Every group with a control row is listed, even one whose tables are all disabled."""
    out: Dict[str, dict] = {}
    for s in states:
        name = s["task_collection"]
        g = out.setdefault(name, {"task_collection": name, "enabled_tables": 0, "no_sla_tables": 0,
                                  **{k: 0 for k in STATES}, "_databases": set(), "_servers": set(), "_late": set(),
                                  "newest_success_at": None})
        g["enabled_tables"] += _count(s, "tables")
        g["no_sla_tables"] += _count(s, "no_sla_tables")
        for k in STATES:
            g[k] += _count(s, k)
        if _count(s, "tables"):
            g["_databases"].add((s["server"], s["src_database"]))
            g["_servers"].add(s["server"])
        if s.get("late_database"):
            g["_late"].add((s["server"], s["src_database"]))
        g["newest_success_at"] = _later(g["newest_success_at"], s.get("newest_success_at"))
    for g in out.values():
        servers = sorted(g.pop("_servers"))
        g["databases"] = len(g.pop("_databases"))
        g["late_databases"] = len(g.pop("_late"))
        g["servers"] = len(servers)
        g["server"] = servers[0] if servers else None
    return out


def servers(states: Iterable[dict], running_groups: Optional[set] = None) -> List[dict]:
    """Per server, by name: its databases (with an enabled table), enabled tables, the ingestion groups
    with a row on it, tables by state, the oldest and newest success, and how many of its groups are
    running now."""
    running_groups = running_groups or set()
    out: Dict[str, dict] = {}
    for s in states:
        v = out.setdefault(s["server"], {"server": s["server"], "tables": 0, **{k: 0 for k in STATES},
                                         "_databases": set(), "_late": set(), "_groups": set(),
                                         "oldest_success_at": None, "newest_success_at": None})
        v["tables"] += _count(s, "tables")
        for k in STATES:
            v[k] += _count(s, k)
        if _count(s, "tables"):
            v["_databases"].add(s["src_database"])
        if s.get("late_database"):
            v["_late"].add(s["src_database"])
        v["_groups"].add(s["task_collection"])
        v["oldest_success_at"] = _earlier(v["oldest_success_at"], s.get("oldest_success_at"))
        v["newest_success_at"] = _later(v["newest_success_at"], s.get("newest_success_at"))
    rows = []
    for v in sorted(out.values(), key=lambda v: str(v["server"])):
        group_names = v.pop("_groups")
        v["databases"] = len(v.pop("_databases"))
        v["late_databases"] = len(v.pop("_late"))
        v["collections"] = len(group_names)
        v["running_collections"] = len(group_names & running_groups)
        rows.append(v)
    return rows


# A source type's port when none is given, as the engine's adapters read it (source_adapters).
DEFAULT_PORTS = {"sqlserver": 1433, "oracle": 1521, "postgresql": 5432, "mysql": 3306}


def endpoint(host: Optional[str], port, kind: Optional[str]) -> str:
    """A server's host and effective port, "host:port", as queries.endpoint_sql spells it (a JDBC server's key)."""
    effective = int(port) if str(port or "").isdigit() and int(port) else DEFAULT_PORTS.get(str(kind or "").lower(), 1433)
    return f"{(host or '').lower()}:{effective}"


def _on_connection(s: dict, name: str, host: str, port=None, kind: Optional[str] = None) -> bool:
    """A states row is on a connection: it names it (whatever the case), or it is a JDBC server at its host and
    port (its server key is that endpoint)."""
    return ((s.get("src_connection") or "").lower() == name.lower()
            or (bool(host) and s.get("server") == endpoint(host, port, kind)))


def connection_databases(states: Iterable[dict], name: str, host: Optional[str], port=None,
                         kind: Optional[str] = None) -> List[dict]:
    """The databases lakefed tracks on one UC connection, by name: control rows naming the connection or
    JDBC rows at its host and port (case-insensitively). Each with its ingestion groups, enabled tables and newest
    success of any of its tables, enabled or not."""
    out: Dict[str, dict] = {}
    for s in states:
        if not _on_connection(s, name, host or "", port, kind):
            continue
        d = out.setdefault(s["src_database"], {"src_database": s["src_database"], "_groups": set(), "tables": 0,
                                               "newest_success_at": None})
        d["_groups"].add(s["task_collection"])
        d["tables"] += _count(s, "tables")
        d["newest_success_at"] = _later(d["newest_success_at"], s.get("newest_any_success_at"))
    rows = []
    for d in sorted(out.values(), key=lambda d: str(d["src_database"])):
        d["collections"] = ", ".join(sorted(d.pop("_groups")))
        rows.append(d)
    return rows


def tracked(states: Iterable[dict], name: str, host: Optional[str], port=None, kind: Optional[str] = None) -> dict:
    """What lakefed tracks on one UC connection: databases, enabled tables and ingestion groups."""
    dbs = connection_databases(states, name, host, port, kind)
    group_names = {g.strip() for d in dbs for g in d["collections"].split(",") if g.strip()}
    return {"databases": len(dbs), "tables": sum(d["tables"] for d in dbs), "collections": len(group_names)}


def _job_sums(name_fields: dict, group_names, states: List[dict]) -> dict:
    rows = [s for s in states if s["task_collection"] in group_names]
    out = {**name_fields, "groups": sorted(group_names), "enabled_tables": 0, "no_sla_tables": 0,
           **{k: 0 for k in STATES}, "newest_success_at": None}
    databases, server_names, late = set(), set(), set()
    for s in rows:
        out["enabled_tables"] += _count(s, "tables")
        out["no_sla_tables"] += _count(s, "no_sla_tables")
        for k in STATES:
            out[k] += _count(s, k)
        if _count(s, "tables"):
            databases.add((s["server"], s["src_database"]))
            server_names.add(s["server"])
        if s.get("late_database"):
            late.add((s["server"], s["src_database"]))
        out["newest_success_at"] = _later(out["newest_success_at"], s.get("newest_success_at"))
    out["databases"], out["servers"], out["late_databases"] = len(databases), len(server_names), len(late)
    return out


def jobs(states: Iterable[dict], schedules: Iterable[dict], members: Iterable[dict], deleting: Iterable[str] = ()):
    """Per ingestion job (a schedule), in the schedules' order: its member groups (active members only) and
    their enabled tables, tables by state, databases and servers. A group in two jobs adds to both. Then
    the groups in no job, summed the same way as "Not in any job", or None when every group is in one.
    `deleting`: the groups of jobs being deleted, which still belong to their job (shown under Deleted jobs) until
    the delete job removes them, so they are never "Not in any job"."""
    states = list(states)
    schedules = list(schedules)
    active: Dict[int, set] = {}
    for m in members:
        if m.get("removed_at") is None:
            active.setdefault(m["schedule_id"], set()).add(m["task_collection"])
    out = [_job_sums({k: s.get(k) for k in ("id", "name", "cadence_minutes", "timezone", "paused", "updated_at",
                                             "parameters", "job_variant", "stagger")},
                     active.get(s["id"], set()), states) for s in schedules]
    scheduled = set().union(*(active.get(s["id"], set()) for s in schedules)) if schedules else set()
    unscheduled = {s["task_collection"] for s in states} - scheduled - set(deleting)
    return out, (_job_sums({"id": None, "name": "Not in any job"}, unscheduled, states) if unscheduled else None)


def sources(states: Iterable[dict], connections: Iterable[dict], not_ingested: Iterable[dict],
            schedules: Iterable[dict], members: Iterable[dict], deleting: Iterable[str] = ()) -> dict:
    """Per source instance (server), by name: how it is read (the UC connections its rows name, else their
    JDBC hosts), its ingestion jobs only (jobs(), over its own rows and groups; groups stay on their job's page), its
    groups, databases, tables by state, and the tables discovered on its hosts with no control row yet. Then
    every readable UC connection that no row names, by name or host, as "nothing registered". Totals: instances
    with tables, connections with nothing registered, and tables discovered and not registered."""
    states, connections, schedules = list(states), list(connections), list(schedules)
    members, not_ingested = list(members), list(not_ingested)
    # UC connection names are case-insensitive: matched lowercased, shown as the connection listing spells them.
    by_name = {c["name"].lower(): c for c in connections}
    used_connections, used_endpoints = set(), set()
    instances = []
    for row in servers(states):
        mine = [s for s in states if s["server"] == row["server"]]
        names = sorted({s["src_connection"].lower() for s in mine if s.get("src_connection")})
        # JDBC paths only from rows read by host (a row reading through its connection may keep an old host).
        hosts = sorted({(s.get("src_host") or "").lower() for s in mine
                        if s.get("src_host") and s["server"] != (s.get("src_connection") or "").lower()})
        # A connection the app cannot list (no rights on it) still shows, with its host marked as not visible.
        read_via = [{"kind": "uc", "name": (by_name.get(n) or {}).get("name") or n,
                     "connection_type": (by_name.get(n) or {}).get("connection_type"),
                     "host": (by_name.get(n) or {}).get("host"), **({} if n in by_name else {"host_hidden": True})}
                    for n in names]
        read_via += [{"kind": "jdbc", "host": h} for h in hosts if not any((v.get("host") or "").lower() == h for v in read_via)]
        # Where it is, host and port: the server itself for a JDBC server, the endpoint of the connection it is.
        endpoints = {endpoint(c.get("host"), c.get("port"), c.get("connection_type"))
                     for c in (by_name.get(row["server"]),) if c and c.get("host")} or {row["server"]}
        used_connections |= set(names)
        used_endpoints |= endpoints
        found = [d for d in not_ingested if d.get("endpoint") in endpoints]
        job_rows, unscheduled = jobs(mine, schedules, members, deleting)
        here = {s["task_collection"] for s in mine}
        # The instance-group labels on its enabled tables (none counts as one): a single label is the server's name;
        # more than one is listed, as the rows call one server by different names.
        labels: Dict[Optional[str], int] = {}
        for s in mine:
            if _count(s, "tables"):
                labels[s.get("instance_label")] = labels.get(s.get("instance_label"), 0) + _count(s, "tables")
        uniform = len(labels) == 1 and next(iter(labels)) is not None
        instances.append({**row, "groups": row["collections"], "read_via": read_via, "nothing_registered": False,
                          "name": next(iter(labels)) if uniform else row["server"],
                          "labels": [] if len(labels) < 2 else [{"label": k, "tables": v} for k, v in
                                                                 sorted(labels.items(), key=lambda kv: str(kv[0] or ""))],
                          "jobs": [dict(j, groups=[g for g in j["groups"] if g in here])
                                   for j in job_rows if j["groups"] and j["enabled_tables"]]
                          + ([unscheduled] if unscheduled else []),
                          "not_ingested": found, "not_registered": sum(_count(d, "not_registered") for d in found)})
    empty = [c for c in connections if c["name"].lower() not in used_connections
             and endpoint(c.get("host"), c.get("port"), c.get("connection_type")) not in used_endpoints]
    for c in empty:
        found = [d for d in not_ingested if d.get("endpoint") == endpoint(c.get("host"), c.get("port"), c.get("connection_type"))]
        instances.append({"server": c["name"], "tables": 0, "databases": 0, "groups": 0, **{k: 0 for k in STATES},
                          "read_via": [{"kind": "uc", "name": c["name"], "connection_type": c.get("connection_type"),
                                        "host": c.get("host")}], "nothing_registered": True, "jobs": [], "labels": [],
                          "name": c["name"],
                          "not_ingested": found, "not_registered": sum(_count(d, "not_registered") for d in found)})
    return {"instances": instances, "ingested": sum(1 for i in instances if i["tables"]),
            "nothing_registered": len(empty), "not_registered": sum(i["not_registered"] for i in instances)}
