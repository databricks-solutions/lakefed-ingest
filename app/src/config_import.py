"""The configuration import: a CSV of what to ingest, imported into one ingestion group.

A CSV line says only where a table is (its server, database, schema and table) and, for a fan-in, the values that
tell its database's rows apart. How to ingest it is the group's: source type, load type, write mode, fan-in, sink
names, freshness target and read options. A group has no record of its own (decision A, no schema change), so its
settings are the values its rows share; a new group, or a setting its rows differ on, is given at import. Pure: no
database access, so main.py reads the group's rows and writes the result."""
import csv
import io
import json
from collections import Counter
from itertools import product
from typing import Dict, List, Optional

import job_settings as js
import queries as q

# The CSV's columns, in export order. Per-line attribute columns are "attr:<sink column>".
CSV_COLUMNS = ("src_connection", "src_host", "src_port", "secret_key", "src_database", "src_schema", "src_table",
               "select_list", "primary_key", "cursor_column", "client_id", "task_collection")
ATTR_PREFIX = "attr:"
MAX_LINES = 5000
# Manage's export of several ingestion jobs says which job each line is in. One import makes one job, so the import
# reads the column only to refuse a file that holds more than one.
JOB_COLUMN = "ingestion_job"
# The export's columns, in order: the job, the import's own (without task_collection, which the import chooses), and
# the load type Create from one CSV reads per line (new_job.UPLOAD_COLUMNS); then each identity column as attr:<name>.
EXPORT_COLUMNS = (JOB_COLUMN,) + tuple(c for c in CSV_COLUMNS if c != "task_collection") + ("load_type",)
# Where a table is read by host and secret. Create from one CSV reads through a connection and refuses these columns,
# so an export leaves them out when none of its tables has one.
JDBC_COLUMNS = ("src_host", "src_port", "secret_key")

# A group's settings, each the control column(s) it fills. sink_* are templates over {database}, {schema}, {table}.
SETTINGS = ("src_type", "use_remote_query", "load_type", "write_mode", "fan_in", "sink_catalog", "sink_schema",
            "sink_table", "freshness_sla_minutes", "jdbc_options")
REQUIRED_SETTINGS = ("src_type", "load_type", "sink_catalog", "sink_schema", "sink_table")
DEFAULTS = {"use_remote_query": False, "write_mode": "merge", "fan_in": False, "freshness_sla_minutes": None,
            "jdbc_options": None}
SINK_SETTINGS = ("sink_catalog", "sink_schema", "sink_table")
BOOLEAN_SETTINGS = ("use_remote_query", "fan_in")
PLACEHOLDERS = ("{database}", "{schema}", "{table}")


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


def _text(value) -> str:
    return "" if value is None else str(value)


def _templates(value: str, row: dict, limit: int = 512) -> set:
    """Every template over {database}, {schema} and {table} that renders to this row's sink name, the literal name
    included. Built over the name's own spans, so a database name that is part of the schema's (Sales in Sales_DB)
    still yields {database}_{schema}."""
    parts = list(zip((q.identifier(row.get("src_database") or ""), q.identifier(row.get("src_schema") or ""),
                      q.identifier(row.get("src_table") or "")), PLACEHOLDERS))
    found: set = set()

    def walk(i: int, built: str):
        if len(found) >= limit:
            return
        if i == len(value):
            found.add(built)
            return
        for part, placeholder in parts:
            if part and value.startswith(part, i):
                walk(i + len(part), built + placeholder)
        walk(i + 1, built + value[i])
    walk(0, "")
    render = lambda t: q.render(t, row.get("src_database") or "", row.get("src_schema") or "", row.get("src_table") or "")
    return {t for t in found if render(t) == value} | {value}


def _preference(template: str) -> tuple:
    """Most placeholders first, then the most different ones, so {database}_{schema} beats {database}_{database}_db."""
    return (sum(template.count(p) for p in PLACEHOLDERS), sum(p in template for p in PLACEHOLDERS), template)


def _setting_of(name: str, row: dict):
    if name == "fan_in":
        return not _blank(row.get("client_id"))
    if name == "use_remote_query":
        return q._truthy(row.get("use_remote_query"))
    value = row.get(name)
    if name == "jdbc_options" and isinstance(value, dict):
        value = json.dumps(value, sort_keys=True)
    return None if _blank(value) else value


def group_settings(rows: List[dict]) -> Dict[str, dict]:
    """Each setting of a group, from its rows: {"value": v} when they share one (None for a group with no rows), or
    {"mixed": {value: rows}} most rows first. A sink name is the template with the most placeholders that every
    row's name fits, so a group writing bronze_<database> reads "bronze_{database}"."""
    out = {}
    for name in SETTINGS:
        if not rows:
            out[name] = {"value": None}
            continue
        if name in SINK_SETTINGS:
            common = set.intersection(*(_templates(_text(r.get(name)), r) for r in rows))
            if common:
                out[name] = {"value": max(common, key=_preference)}
                continue
        values = Counter(_setting_of(name, r) for r in rows)
        if len(values) == 1:
            out[name] = {"value": next(iter(values))}
        else:
            out[name] = {"mixed": {_text(v): n for v, n in values.most_common()}}
    return out


def effective_settings(given: dict, group_rows: List[dict]):
    """(settings, errors): each setting as given for this import, else the group's shared value, else its default.
    A required setting nobody gave, or one the group's rows differ on, is an error naming it."""
    shared = group_settings(group_rows)
    settings = {}
    errors = [f"unknown setting {name}" for name in given if name not in SETTINGS and name not in js.RETIRED_SETTINGS]
    for name in SETTINGS:
        if name in given and not (_blank(given[name]) and name in REQUIRED_SETTINGS):
            settings[name] = given[name]
            if name in BOOLEAN_SETTINGS and given[name] is not None:
                try:
                    settings[name] = q.parse_bool(given[name])
                except ValueError as e:
                    errors.append(f"{name}: {e}")
        elif "mixed" in shared[name]:
            errors.append(f"{name} differs across the group's rows ({', '.join(shared[name]['mixed'])}): "
                          "set it for this import")
        elif shared[name]["value"] is not None or name not in REQUIRED_SETTINGS:
            settings[name] = shared[name]["value"] if shared[name]["value"] is not None else DEFAULTS.get(name)
        else:
            errors.append(f"set {name} for this new group" if not group_rows else f"set {name} for this import")
    return settings, errors


def _header_errors(header: List[str]) -> List[str]:
    errors = [f"column {c} is given twice" for c, n in Counter(header).items() if n > 1]
    for column in header:
        if column in CSV_COLUMNS or column == JOB_COLUMN or (
                column.startswith(ATTR_PREFIX) and q._ATTR_NAME.match(column[len(ATTR_PREFIX):])):
            continue
        hint = ": a group setting, set at import" if column in SETTINGS else ""
        errors.append(f"unknown column {column}{hint}")
    return errors


def _row(line: dict, group: str, settings: dict, unity_catalog: bool = False) -> dict:
    database, schema, table = (line.get(k, "").strip() for k in ("src_database", "src_schema", "src_table"))
    # Identity values are written as given (the sink keeps them as text); a blank one is null, never dropped.
    attrs = {k[len(ATTR_PREFIX):]: (None if v == "" else v) for k, v in line.items() if k.startswith(ATTR_PREFIX)}
    row = {k: (line.get(k) or "").strip() or None for k in CSV_COLUMNS if k != "task_collection"}
    row.update({
        "task_collection": group, "src_type": settings["src_type"], "load_type": settings["load_type"],
        "use_remote_query": bool(settings["use_remote_query"]), "write_mode": settings["write_mode"] or "merge",
        "freshness_sla_minutes": settings["freshness_sla_minutes"], "jdbc_options": settings["jdbc_options"],
        "client_attrs": json.dumps(attrs) if attrs else None,
    })
    for name in SINK_SETTINGS:
        row[name] = q.render(settings[name], database, schema, table, unity_catalog=unity_catalog)
    if row["src_port"] is not None and row["src_port"].isdigit():
        row["src_port"] = int(row["src_port"])
    return row


def _source_errors(row: dict, settings: dict) -> List[str]:
    errors = []
    port = row.get("src_port")
    if port is not None and not (isinstance(port, int) and 1 <= port <= 65535):
        errors.append("src_port must be a whole number from 1 to 65535")
    if settings["use_remote_query"]:
        errors += [f"{c} must be blank: this group reads through the UC connection"
                   for c in ("src_host", "src_port", "secret_key") if not _blank(row.get(c))]
    return errors


def _lines_within(rows, max_lines: int) -> Optional[List[dict]]:
    """The file's non-blank lines, read one at a time. None as soon as there are more than max_lines, so an oversize
    file is refused without reading the rest of it."""
    lines: List[dict] = []
    for row in rows:
        if not any(not _blank(v) for v in row.values()):
            continue
        if len(lines) == max_lines:
            return None
        lines.append(dict(row))
    return lines


def plan_import(text: str, group: str, given: dict, group_rows: List[dict], max_lines: int = MAX_LINES,
                check_rows: bool = True, unity_catalog: bool = False) -> dict:
    """What importing this CSV into the group would write: {"errors": the file's own problems, "lines": one per
    line, with its file line number, the control row it makes, its status (valid, invalid or registered, for a
    table the group already has) and its errors}. Nothing is valid while the file has errors. check_rows=False leaves
    out the form checks of each row, for a caller that checks the row itself once its settings are applied."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    errors = _header_errors(header) if header else ["the file is empty"]
    if errors:
        return {"errors": errors, "lines": [], "settings": {}}
    lines = _lines_within(reader, max_lines)
    if lines is None:
        return {"errors": [f"more than {max_lines:,} lines: at most {max_lines:,} in one import"], "lines": [],
                "settings": {}}
    named = sorted({(line.get(JOB_COLUMN) or "").strip() for line in lines} - {""})
    if len(named) > 1:
        return {"errors": [f"this file holds {len(named)} ingestion jobs ({', '.join(named[:5])}): one import makes "
                           f"one job, so import each job's lines on their own"], "lines": [], "settings": {}}
    settings, errors = effective_settings(given, group_rows)
    if errors:
        return {"errors": errors, "lines": [], "settings": settings}
    typed = js.typed_name_errors(settings, unity_catalog)
    if typed:
        return {"errors": typed, "lines": [], "settings": settings}
    have = {(".".join(_text(r.get(k)).lower() for k in SINK_SETTINGS), _text(r.get("client_id"))) for r in group_rows}
    seen: Dict[tuple, int] = {}
    out = []
    for number, line in enumerate(lines, start=2):
        surplus = None in line
        line = {k: v or "" for k, v in line.items() if k is not None}
        row = _row(line, group, settings, unity_catalog)
        problems = ["this line has more cells than the header has columns"] if surplus else []
        pinned = line.get("task_collection", "").strip()
        if pinned and pinned != group:
            problems.append(f"task_collection is {pinned}, but this import is into {group}")
        if settings["fan_in"] and _blank(row["client_id"]):
            problems.append("client_id is required: this group fans in to shared tables")
        if not settings["fan_in"] and not _blank(row["client_id"]):
            problems.append("client_id must be blank: this group does not fan in")
        problems += _source_errors(row, settings)
        if check_rows:
            problems += [e for e in q.validate_control_row(row) if not e.startswith("task_collection")]
        sink = ".".join(_text(row.get(k)).lower() for k in SINK_SETTINGS)
        key = (sink, _text(row.get("client_id")))
        if key in seen:
            problems.append(f"line {seen[key]} already writes {sink}" + (f" for {key[1]}" if key[1] else ""))
        seen.setdefault(key, number)
        status = "invalid" if problems else "registered" if key in have else "valid"
        out.append({"line": number, "table": ".".join((row["src_database"] or "", row["src_schema"] or "",
                                                       row["src_table"] or "")),
                    "sink": sink, "row": row, "status": status, "errors": problems})
    return {"errors": [], "lines": out, "settings": settings}


def export_csv(rows: List[dict]) -> str:
    """A group's rows as the import CSV: the columns any row uses, in CSV_COLUMNS order, then its attribute columns.
    task_collection is left out, as the import chooses the group."""
    attrs = []
    for r in rows:
        value = r.get("client_attrs")
        for k in (json.loads(value) if isinstance(value, str) and value else value or {}):
            if k not in attrs:
                attrs.append(k)
    columns = [c for c in CSV_COLUMNS if c != "task_collection" and any(not _blank(r.get(c)) for r in rows)]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns + [ATTR_PREFIX + a for a in attrs])
    for r in rows:
        value = r.get("client_attrs")
        mine = json.loads(value) if isinstance(value, str) and value else value or {}
        cell = lambda v: "" if v is None else v if isinstance(v, str) else json.dumps(v)     # true, 5: as JSON writes them
        writer.writerow([_text(r.get(c)) for c in columns] + [cell(mine.get(a)) for a in attrs])
    return buffer.getvalue()


def export_columns(jdbc: bool) -> tuple:
    """The export's columns before its identity columns: EXPORT_COLUMNS, less JDBC_COLUMNS when no table needs them."""
    return EXPORT_COLUMNS if jdbc else tuple(c for c in EXPORT_COLUMNS if c not in JDBC_COLUMNS)


def export_header(columns: tuple, attrs: List[str]) -> str:
    """The export's header line: the columns, then attr:<name> for each identity column."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(list(columns) + [ATTR_PREFIX + a for a in attrs])
    return buffer.getvalue()


def _attr_cell(value) -> str:
    return "" if value is None else value if isinstance(value, str) else json.dumps(value)


def export_lines(rows: List[dict], columns: tuple, attrs: List[str]) -> str:
    """Control rows (each with its ingestion_job) as export lines under export_header(columns, attrs), in the import's
    own spelling: a blank for nothing, and true or 5 as JSON writes them in an identity column."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for r in rows:
        value = r.get("client_attrs")
        mine = json.loads(value) if isinstance(value, str) and value else value or {}
        writer.writerow([_text(r.get(c)) for c in columns] + [_attr_cell(mine.get(a)) for a in attrs])
    return buffer.getvalue()


def setting_summary(settings: Dict[str, dict]) -> List[dict]:
    """The group page's Settings rows: each setting's value, or its mixed values with their row counts."""
    return [{"setting": name, **settings[name]} for name in SETTINGS]
