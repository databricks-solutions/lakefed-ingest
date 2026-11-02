"""An ingestion job's settings: what all of its tables share, read from their control rows and edited for all of
them at once (decision A: settings live on the rows, no table of their own).

Target names, client_id and identity values are templates over four tags, {server}, {database}, {schema} and
{table}, filled from each table's own row. A job keeps two write modes apart: one for change-tracked tables and one
for full reloads (cursor and cdc tables always merge). Pure: main.py reads the rows and applies the per-row changes through
queries.plan_edit."""
import json
import re
from collections import Counter
from typing import Dict, List, Optional

import placement
import queries as q

TAGS = ("{server}", "{database}", "{schema}", "{table}")
VALID_TAGS = ", ".join(TAGS)
TARGET_SETTINGS = (("sink_catalog", "target catalog"), ("sink_schema", "target schema"), ("sink_table", "target table"))
# Settings the app no longer takes: a target name follows its naming rule alone. A request or file that still names one
# (an older client, a saved request) is read and ignored, so it is neither refused nor written.
RETIRED_SETTINGS = ("lowercase",)
CHANGE_LOAD_TYPES = ("ct",)     # append mode is for ct and full only; cursor and cdc tables always merge
IDENTITY_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# The client_id of a job's shared rows when its settings don't say: the database name alone, as every job had before
# new jobs built theirs from the server too (new_job.py).
LEGACY_CLIENT_ID = "{database}"
# The client_id of a new fan-in job's rows. Two servers can hold a database of the same name, so the server goes in
# too; the underscore keeps the key to the characters queries.CLIENT_ID_PATTERN allows.
SHARED_CLIENT_ID = "{server}_{database}"
# The readable identity columns a new fan-in job's rows get, so a shared table's rows say which server and database
# they came from. The New job form shows them and the user may remove them.
SHARED_IDENTITY = {"source_server": "{server}", "source_database": "{database}"}
TARGET_NAMES_OPTION = "target_names"      # a schedule option: how a job's target names are made
UNITY_CATALOG_NAMES = "unity_catalog"     # what Create stores from now on; a job without the option keeps the old rule


def server_of(row: dict) -> str:
    """The server a row reads, exactly as queries.SERVER names it: its UC connection when it reads through one (or
    has no host), else its host and effective port, lower case."""
    host = row.get("src_host")
    if row.get("src_connection") is not None and (q._truthy(row.get("use_remote_query")) or host is None):
        return str(row["src_connection"]).lower()
    if host is None:
        return ""
    port = row.get("src_port") or q.DEFAULT_PORTS.get(str(row.get("src_type") or "sqlserver").lower(), 1433)
    return f"{str(host).lower()}:{int(port)}"


def _server_key(name: str, src_type: str = "sqlserver", known=()) -> str:
    """A server as typed in a file or by hand, named as server_of names it: a host without a port gets the
    engine's default one. A name with no dot reads as a Unity Catalog connection's, unless it is a host the job already
    reads (in known, the job's servers) at its default port: a dotless host name such as sqlprod01 is one too."""
    name = name.strip().lower()
    with_port = f"{name}:{q.DEFAULT_PORTS.get(src_type, 1433)}"
    if ":" not in name and (re.search(r"[.]", name) or (name not in known and with_port in known)):
        return with_port
    return name


def _values(row: dict) -> Dict[str, str]:
    return {"{server}": server_of(row), "{database}": str(row.get("src_database") or ""),
            "{schema}": str(row.get("src_schema") or ""), "{table}": str(row.get("src_table") or "")}


def unity_catalog_of(options: Optional[dict]) -> bool:
    """Whether a job's target names follow Unity Catalog's rules. A job made before they did has no setting for it and
    keeps the older identifier rule, so its names come out as they always have."""
    return (options or {}).get(TARGET_NAMES_OPTION) == UNITY_CATALOG_NAMES


def _fill(value: str, target: bool, unity_catalog: bool = False) -> str:
    """A tag's value as it goes into a name. A target name is lower case: by Unity Catalog's rules for a job with the
    setting, else by the older identifier rule. Other templates keep the value as it is."""
    if not target:
        return value
    if unity_catalog:
        return q.unity_catalog_identifier(value)
    return q.identifier(value)


def render(template: Optional[str], row: dict, target: bool = False, unity_catalog: bool = False) -> Optional[str]:
    """A template filled from one row, each tag's value as _fill gives it for a target name or as it is otherwise."""
    if template is None or not str(template).strip():
        return None
    out = str(template).strip()
    for tag, value in _values(row).items():
        out = out.replace(tag, _fill(value, target, unity_catalog))
    return out


def typed_name_errors(settings: dict, unity_catalog: bool) -> List[str]:
    """Text typed into a target name, outside its tags, is kept as typed. Under Unity Catalog's rules it must not hold a
    character Unity Catalog refuses, since no table could be made with that name."""
    if not unity_catalog:
        return []
    return [f"{label} {settings.get(field)!r} has a character Unity Catalog refuses (a space, period, slash or control "
            "character): take it out"
            for field, label in TARGET_SETTINGS
            if q.UNITY_CATALOG_REJECTED.search(re.sub(r"\{[^{}]*\}", "", str(settings.get(field) or "")))]


def template_errors(template: Optional[str], label: str, fan_in: bool = False) -> List[str]:
    errors = [f"{label}: unknown tag {tag}; valid tags are {VALID_TAGS}"
              for tag in re.findall(r"\{[^{}]*\}", str(template or "")) if tag not in TAGS]
    if fan_in:
        for tag, each in (("{server}", "server"), ("{database}", "database")):
            if tag in str(template or ""):
                errors.append(f"{label}: {tag} gives every {each} its own table, so fan-in can't use it")
    return errors


def _templates(value: str, row: dict, target: bool, limit: int = 512, unity_catalog: bool = False) -> set:
    """Every template over the four tags that fills to this value for this row, the literal value included."""
    parts = [(_fill(v, target, unity_catalog=unity_catalog), tag) for tag, v in _values(row).items()]
    found: set = set()

    def walk(i: int, built: str):
        if len(found) >= limit:
            return
        if i == len(value):
            found.add(built)
            return
        for part, tag in parts:
            if part and value.startswith(part, i):
                walk(i + len(part), built + tag)
        walk(i + 1, built + value[i])
    walk(0, "")
    return {t for t in found if render(t, row, target=target, unity_catalog=unity_catalog) == value} | {value}


def _preference(template: str) -> tuple:
    return (sum(template.count(t) for t in TAGS), sum(t in template for t in TAGS), template)


def _shared_template(values_and_rows, target: bool, unity_catalog: bool = False) -> Optional[str]:
    """The template every row's value fits, most tags first. The first row's candidates are tried in that order
    against every row, so a job of millions of rows is checked with one fill per row, not one search."""
    pairs = list(values_and_rows)
    if not pairs:
        return None
    first_value, first_row = pairs[0]
    for candidate in sorted(_templates(str(first_value), first_row, target, unity_catalog=unity_catalog),
                            key=_preference, reverse=True):
        if all(render(candidate, row, target=target, unity_catalog=unity_catalog) == str(value)
               or (candidate == str(value) == "")
               for value, row in pairs):
            return candidate
    return None


def _ranked(value: str, row: dict, target: bool, unity_catalog: bool = False) -> List[str]:
    """Every template that fills to this value for this row, most tags first: the order to try them in."""
    return sorted(_templates(value, row, target, unity_catalog=unity_catalog), key=_preference, reverse=True)


def _shared(values) -> dict:
    counts = Counter(values)
    if len(counts) <= 1:
        return {"value": next(iter(counts), None)}
    return {"mixed": {("" if v is None else str(v)): n for v, n in counts.most_common()}}


def _attrs(row: dict) -> dict:
    value = row.get("client_attrs")
    if isinstance(value, dict):
        return value
    return json.loads(value) if value else {}


def differing_identity_columns(identity: dict, tables: int) -> List[str]:
    """Identity columns every table has, but with values that follow no one template, so the page can show each as
    its own row. The read reports such a column as one mixed entry naming it and covering every table; tables whose
    identity columns themselves differ give one entry per set of names (joined with ", ") instead."""
    mixed = (identity or {}).get("mixed") or {}
    if len(mixed) != 1 or not tables:
        return []
    name, count = next(iter(mixed.items()))
    return [name] if count == tables and ", " not in name else []


def read(rows: List[dict], unity_catalog: bool = False) -> Dict[str, dict]:
    """Each setting of a job, from its rows: {"value": v} when they share it, else {"mixed": {value: rows}}. Target
    names are read by the job's naming rule: Unity Catalog's when unity_catalog, else the older identifier rule."""
    out: Dict[str, dict] = {}
    fan = [bool(str(r.get("client_id") or "").strip()) for r in rows]
    out["fan_in"] = _shared(fan)
    changes = [r for r in rows if r.get("load_type") in CHANGE_LOAD_TYPES]
    full = [r for r in rows if r.get("load_type") == "full"]
    out["write_mode_changes"] = _shared(str(r.get("write_mode") or "merge") for r in changes)
    out["write_mode_full"] = _shared(str(r.get("write_mode") or "merge") for r in full)
    out["freshness_sla_minutes"] = _shared(r.get("freshness_sla_minutes") for r in rows)
    out["unity_catalog"] = {"value": unity_catalog}
    for field, _ in TARGET_SETTINGS:
        shared = _shared_template(((r.get(field) or "", r) for r in rows), target=True,
                                  unity_catalog=unity_catalog) if rows else None
        out[field] = {"value": shared} if shared is not None or not rows else _shared(r.get(field) for r in rows)
    with_id = [r for r in rows if str(r.get("client_id") or "").strip()]
    shared_id = _shared_template(((r["client_id"], r) for r in with_id), target=False) if with_id else None
    out["client_id"] = {"value": shared_id} if shared_id is not None or not with_id else _shared(r["client_id"] for r in with_id)
    out["identity"] = _identity(rows)
    return out


def _identity(rows: List[dict]) -> dict:
    attrs = [(_attrs(r), r) for r in rows]
    columns = Counter(tuple(a) for a, _ in attrs)
    if len(columns) > 1:
        return {"mixed": {", ".join(c) or "(none)": n for c, n in columns.most_common()}}
    names = next(iter(columns), ())
    shared = {}
    for name in names:
        template = _shared_template(((a[name] if a[name] is not None else "", r) for a, r in attrs), target=False)
        if template is None:
            return {"mixed": {name: len(rows)}}
        shared[name] = template
    return {"value": shared}


def _current(read_settings: dict, name: str):
    return read_settings.get(name, {}).get("value")


def default_client_id(fan_in_now) -> str:
    """The client_id template a settings edit fills when it names none and the rows share none: a job turning fan-in
    on takes the server-and-database key new fan-in jobs get, so two servers' databases of one name keep apart; a job
    already sharing its tables keeps the database-only key it has always had."""
    return LEGACY_CLIENT_ID if fan_in_now else SHARED_CLIENT_ID


def _desired(row: dict, settings: dict, now: dict) -> dict:
    want: Dict[str, object] = {}
    fan_in = settings.get("fan_in", bool(str(row.get("client_id") or "").strip()))
    if "fan_in" in settings or "client_id" in settings:
        template = (settings.get("client_id") or _current(now, "client_id")
                    or default_client_id(_current(now, "fan_in")))
        want["client_id"] = render(template, row) if fan_in else None
    load_type = row.get("load_type")
    if "write_mode_changes" in settings and load_type in CHANGE_LOAD_TYPES:
        want["write_mode"] = settings["write_mode_changes"]
    if "write_mode_full" in settings and load_type == "full":
        want["write_mode"] = settings["write_mode_full"]
    if "freshness_sla_minutes" in settings:
        want["freshness_sla_minutes"] = settings["freshness_sla_minutes"]
    unity_catalog = bool(_current(now, "unity_catalog"))
    for field, _ in TARGET_SETTINGS:
        template = settings.get(field)
        if template:
            want[field] = render(template, row, target=True, unity_catalog=unity_catalog)
    if "identity" in settings:
        filled = {name: render(t, row) for name, t in (settings["identity"] or {}).items()}
        want["client_attrs"] = json.dumps(filled) if filled else None
    return want


def plan(rows: List[dict], settings: dict, unity_catalog: bool = False) -> Dict[int, dict]:
    """{row id: {field: new value}} for every row a settings edit changes; rows it leaves as they are are left out.
    A setting not given keeps each row's value. unity_catalog is the job's naming rule (unity_catalog_of)."""
    now = read(rows, unity_catalog)
    out: Dict[int, dict] = {}
    for row in rows:
        changes = {}
        for field, value in _desired(row, settings, now).items():
            value = q.normalize_value(field, value)
            if not q._same(field, row.get(field), value, row):
                changes[field] = value
        if changes:
            out[row["id"]] = changes
    return out


def check_templates(settings: dict, fan_in: bool) -> List[str]:
    """The checks of a settings edit that need no rows: unknown tags, {server} or {database} in fan-in target
    names, and identity column names."""
    errors: List[str] = []
    for key, label in (("write_mode_changes", "change-tracked"), ("write_mode_full", "full-reload")):
        if key in settings and str(settings[key] or "").strip().lower() not in q.WRITE_MODES:
            errors.append(f"write mode for {label} tables: merge or append")
    if "freshness_sla_minutes" in settings and settings["freshness_sla_minutes"] not in (None, ""):
        if not str(settings["freshness_sla_minutes"]).strip().isdigit() or int(settings["freshness_sla_minutes"]) < 1:
            errors.append("freshness target: a whole number of minutes, 1 or more, or none")
    for field, label in TARGET_SETTINGS:
        if field in settings:
            errors += template_errors(settings[field], label, fan_in=bool(fan_in))
    if "client_id" in settings:
        errors += template_errors(settings["client_id"], "client_id")
    seen: set = set()
    for name, template in (settings.get("identity") or {}).items():
        if not IDENTITY_COLUMN.match(name) or name.startswith("__"):
            errors.append(f"identity column {name}: letters, digits and _, not starting with __")
        elif name.lower() in seen:
            errors.append(f"identity column {name} is given twice (names ignore case)")
        seen.add(name.lower())
        errors += template_errors(template, f"identity column {name}")
    return errors


def check(rows: List[dict], settings: dict, unity_catalog: bool = False) -> List[str]:
    """What is wrong with a settings edit before any row is planned: bad tags, bad column names, and, without
    fan-in, two tables writing one target."""
    now = read(rows, unity_catalog)
    fan_in = settings.get("fan_in", _current(now, "fan_in"))
    errors = check_templates(settings, fan_in=bool(fan_in))
    if errors or fan_in:
        return errors
    changes = plan(rows, settings, unity_catalog)
    targets: Dict[str, int] = {}
    for row in rows:
        merged = {**row, **changes.get(row["id"], {})}
        name = ".".join(str(merged.get(f)) for f, _ in TARGET_SETTINGS).lower()
        targets[name] = targets.get(name, 0) + 1
    shared = [name for name, n in targets.items() if n > 1]
    if shared:
        errors.append(f"{len(shared)} targets would be written by more than one table (e.g. {shared[0]}): "
                      "add {database} to the target names, or turn fan-in on")
    return errors


# ---- adding databases to a job ------------------------------------------------------------------

DATABASE_FIELDS = ("server", "database", "enabled")
_SERVER_COPY = ("src_type", "src_connection", "use_remote_query", "src_host", "src_port", "secret_key")
_TABLE_COPY = ("src_type", "src_schema", "src_table", "select_list", "primary_key", "load_type", "cursor_column",
               "write_mode", "freshness_sla_minutes", "jdbc_options", "load_partitioned", "partition_col",
               "partition_size_mb", "tier", "priority")


def read_databases(text: str, columns: Dict[str, str]):
    """(entries, errors): one entry per line of a databases file, its columns matched to server, database and
    (optionally) enabled through `columns` ({field: file column}). Other columns are ignored."""
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    errors = [f"the file has no column {columns[f]} (matched to {f})" for f in DATABASE_FIELDS
              if f in columns and columns[f] not in header]
    errors += [f"match a file column to {f}" for f in ("server", "database") if f not in columns]
    if errors:
        return [], errors
    entries = []
    for record in reader:
        server = str(record.get(columns["server"]) or "").strip()
        database = str(record.get(columns["database"]) or "").strip()
        if not server or not database:
            errors.append(f"line {reader.line_num}: server and database are required")
            continue
        enabled = q._truthy(record.get(columns["enabled"])) if "enabled" in columns else True
        entries.append({"line": reader.line_num, "server": server, "database": database, "enabled": enabled})
    return entries, errors


def _bucket(database: str, count: int) -> int:
    import zlib
    return zlib.crc32(database.lower().encode("utf-8")) % max(1, int(count))


def plan_databases(job_rows: List[dict], entries: List[dict], job_name: str, groups_per_server: int,
                   full_list: bool = False, unity_catalog: bool = False) -> dict:
    """What adding these databases does to a job: {new: control rows to insert, enable / disable: row ids,
    new_groups, errors}. A new database gets every table of the job, each copied from the table's first row (load
    type, key, columns, write mode) and given the job's target names, client_id and identity columns, filled for
    it. It joins its server's group with the fewest tables, or, on a server new to the job, one of groups_per_server
    new groups, the server's new databases spread evenly over them. A database already in the job is left, its tables enabled or disabled as the
    file says; with full_list, the job's databases missing from the file are disabled, never deleted."""
    out = {"new": [], "enable": [], "disable": [], "new_groups": [], "errors": []}
    if not job_rows:
        out["errors"].append("the job has no tables yet: import one database's tables first (Import CSV)")
        return out
    now = read(job_rows, unity_catalog)
    for name in ("sink_catalog", "sink_schema", "sink_table", "identity"):
        if "mixed" in now[name]:
            out["errors"].append(f"the job's {name} differs between its tables: set it on the job first")
    if "mixed" in now["fan_in"] or "mixed" in now["client_id"]:
        out["errors"].append("the job's fan-in or client_id differs between its tables: set it on the job first")
    if out["errors"]:
        return out
    models: Dict[tuple, dict] = {}
    by_database: Dict[tuple, List[dict]] = {}
    servers: Dict[str, dict] = {}
    groups: Dict[str, set] = {}
    loads: Dict[str, Dict[str, int]] = {}
    for r in sorted(job_rows, key=lambda r: r["id"]):
        server = server_of(r).lower()
        models.setdefault((str(r["src_schema"]).lower(), str(r["src_table"]).lower()), r)
        by_database.setdefault((server, str(r["src_database"] or "").lower()), []).append(r)
        servers.setdefault(server, r)
        groups.setdefault(server, set()).add(r["task_collection"])
        loads.setdefault(server, {}).setdefault(r["task_collection"], 0)
        loads[server][r["task_collection"]] += 1
    lightest = placement.Lightest(loads)
    # A server new to the job: its new databases, each taking the job's tables, spread evenly over its new groups.
    src_type = str(next(iter(models.values())).get("src_type") or "sqlserver").lower()
    new_entries = [(_server_key(e["server"], src_type, servers), e["database"]) for e in entries]
    new_entries = [(server, database) for server, database in new_entries if server not in groups]
    new_tables: Dict[str, int] = {}
    for server, _ in new_entries:
        new_tables[server] = new_tables.get(server, 0) + len(models)
    new_spread = placement.server_placements(((server, database, len(models)) for server, database in new_entries),
                                             {server: placement.group_count(tables, groups_per_server)
                                              for server, tables in new_tables.items()})
    known = set(by_database)
    listed: Dict[tuple, bool] = {}
    secret_templates: Dict[str, Optional[str]] = {}
    new_groups: set = set()
    for e in entries:
        key = (_server_key(e["server"], str(next(iter(models.values())).get("src_type") or "sqlserver").lower(), servers),
               e["database"].lower())
        if key in listed:
            if listed[key] != e["enabled"]:
                out["errors"].append(f"line {e['line']}: {key[0]} / {key[1]} is listed again with another "
                                     "enabled value")
            continue
        listed[key] = e["enabled"]
        if key in known:
            for r in by_database[key]:
                if bool(r.get("task_enabled")) != e["enabled"]:
                    out["enable" if e["enabled"] else "disable"].append(r["id"])
            continue
        source = servers.get(key[0])
        if source is None and re.search(r"[.:]", e["server"]):
            out["errors"].append(f"line {e['line']}: server {e['server']} is new to this job and is not a UC "
                                 "connection name; add one of its databases by Import CSV first, with its secret")
            continue
        if source is not None:
            reach = {f: source.get(f) for f in _SERVER_COPY}
            if source.get("secret_key"):
                # A JDBC database's secret is usually its own (Bulk import's {database}): follow the server's
                # pattern, so a new database never borrows another's credentials.
                secrets = secret_templates.setdefault(key[0], _shared_template(
                    ((r.get("secret_key") or "", r) for rs in (v for k, v in by_database.items() if k[0] == key[0])
                     for r in rs), target=False))
                if secrets is None:
                    out["errors"].append(f"line {e['line']}: the secrets of server {e['server']} follow no single "
                                         "pattern; add this database by Import CSV, with its secret")
                    continue
                reach["secret_key"] = render(secrets, {**source, "src_database": e["database"]})
            group = lightest.place(key[0], e["database"], len(models))
        else:
            reach = {"src_connection": e["server"], "use_remote_query": True, "src_host": None, "src_port": None,
                     "secret_key": None}
            index = new_spread.get(key[0], {}).get(str(e["database"]).lower(), 0)
            group = f"{q.identifier(job_name)}_{q.identifier(e['server'])}_c{index + 1:02d}"
            new_groups.add(group)
        planned = []
        for model in models.values():
            row = {f: model.get(f) for f in _TABLE_COPY}
            row.update(reach, src_database=e["database"], task_collection=group, task_enabled=e["enabled"])
            if not row.get("src_type"):
                row["src_type"] = model.get("src_type")
            for field, _ in TARGET_SETTINGS:
                row[field] = render(now[field]["value"], row, target=True, unity_catalog=unity_catalog)
            row["client_id"] = render(now["client_id"]["value"] or LEGACY_CLIENT_ID, row) if now["fan_in"]["value"] else None
            identity = {name: render(t, row) for name, t in (now["identity"]["value"] or {}).items()}
            row["client_attrs"] = json.dumps(identity) if identity else None
            planned.append(row)
        problems = sorted({p for row in planned for p in q.validate_control_row(row)})
        if problems:
            out["errors"].append(f"line {e['line']}: {e['database']}: " + "; ".join(problems))
            continue
        out["new"] += planned
    if full_list:
        for key, rows in by_database.items():
            if key not in listed:
                out["disable"] += [r["id"] for r in rows if r.get("task_enabled")]
    out["disable"] = sorted(set(out["disable"]))
    out["enable"] = sorted(set(out["enable"]))
    out["new_groups"] = sorted(new_groups)
    return out
