"""An ingestion job's settings, read and edited in set-based SQL, so a job of millions of tables is one scan to read
and one statement to change: no Python object per row, and one change-log line per database instead of per table.

The meaning is job_settings.py's (job_settings_sql_pg_test.py checks they agree): templates over {server},
{database}, {schema} and {table} filled from each row, two write modes kept apart, a reseed where a change-tracked or
cursor table gets a new target or client_id. Every query reads alias t = control, limited to the job's groups."""
import json
import re
from collections import Counter
from typing import Callable, Dict, List, Optional

import job_settings as js
import queries as q

S = q.SCHEMA
WHERE = "t.task_collection = any(%(groups)s)"
TAG_SQL = {"{server}": f"({q.SERVER})", "{database}": "coalesce(t.src_database, '')",
           "{schema}": "coalesce(t.src_schema, '')", "{table}": "coalesce(t.src_table, '')"}
# queries.identifier in SQL: lower case, runs of other characters to one _, trimmed, "t" when nothing is left.
IDENT = "coalesce(nullif(btrim(regexp_replace(lower({x}), '[^a-z0-9_]+', '_', 'g'), '_'), ''), 't')"
# queries.unity_catalog_identifier in SQL: each character Unity Catalog refuses becomes _, then lower case.
IDENT_UC = r"lower(regexp_replace({x}, '[. /\x01-\x1f\x7f]', '_', 'g'))"
CLIENT_ID_RE = q.CLIENT_ID_PATTERN
HAS_ID = "nullif(btrim(t.client_id), '') is not null"
CANDIDATES_PER_SCAN = 16


class Params(dict):
    """Named query parameters, each value under a fresh name, so one expression can be placed twice."""
    def add(self, value) -> str:
        name = f"p{len(self)}"
        self[name] = value
        return f"%({name})s"


def render_sql(template: Optional[str], p: Params, target: bool = False, unity_catalog: bool = False) -> str:
    """SQL filling a template from alias t, as job_settings.render does. A blank template is NULL."""
    text = str(template or "").strip()
    if not text:
        return "null::text"
    name_rule = IDENT_UC if target and unity_catalog else IDENT if target else None
    parts = []
    for piece in re.split(r"(\{server\}|\{database\}|\{schema\}|\{table\})", text):
        if piece in TAG_SQL:
            parts.append(name_rule.format(x=TAG_SQL[piece]) if name_rule else TAG_SQL[piece])
        elif piece:
            parts.append(f"{p.add(piece)}::text")
    return "(" + " || ".join(parts) + ")"


def _attrs_sql(identity: Dict[str, str], p: Params) -> str:
    """The client_attrs text for an identity mapping, spelled as json.dumps spells it; NULL when empty."""
    if not identity:
        return "null::text"
    pieces = []
    for i, (name, template) in enumerate(identity.items()):
        pieces.append(f"{p.add(('' if i == 0 else ', ') + json.dumps(name) + ': ')}::text || "
                      f"coalesce(to_json({render_sql(template, p)})::text, 'null')")
    return "('{' || " + " || ".join(pieces) + " || '}')"


def _shared_counts(counts: Counter) -> dict:
    if len(counts) <= 1:
        return {"value": next(iter(counts), None)}
    return {"mixed": {("" if v is None else str(v)): n for v, n in counts.most_common()}}


def read(fetch: Callable, groups: List[str], unity_catalog: bool = False) -> Dict[str, dict]:
    """job_settings.read for a whole job, in SQL. `fetch(sql, params)` returns rows as dicts."""
    base = fetch(f"""select count(*) as n, count(*) filter (where {HAS_ID}) as with_id,
        min(t.id) as first_id, min(t.id) filter (where {HAS_ID}) as first_with_id
        from {S}.control t where {WHERE}""", {"groups": groups})[0]
    if not base["n"]:
        return js.read([], unity_catalog)
    n, with_id = base["n"], base["with_id"]
    out: Dict[str, dict] = {"fan_in": _shared_counts(Counter({True: with_id, False: n - with_id}) - Counter())}
    mix = fetch(f"""select t.load_type, coalesce(t.write_mode, 'merge') as write_mode, t.freshness_sla_minutes as sla,
        count(*) as n from {S}.control t where {WHERE} group by 1, 2, 3""", {"groups": groups})
    changes, full, sla = Counter(), Counter(), Counter()
    for m in mix:
        if m["load_type"] in js.CHANGE_LOAD_TYPES:
            changes[m["write_mode"]] += m["n"]
        if m["load_type"] == "full":
            full[m["write_mode"]] += m["n"]
        sla[m["sla"]] += m["n"]
    out["write_mode_changes"], out["write_mode_full"] = _shared_counts(changes), _shared_counts(full)
    out["freshness_sla_minutes"] = _shared_counts(sla)
    out["unity_catalog"] = {"value": unity_catalog}
    sample = fetch(q.control_row_sql(), (base["first_id"],))[0]
    id_sample = sample if base["first_with_id"] in (None, base["first_id"]) else \
        fetch(q.control_row_sql(), (base["first_with_id"],))[0]
    attrs = js._attrs(sample)

    # Each field's candidate templates, most tags first, from one row; then one scan checks the first of each.
    fields: Dict[str, dict] = {}
    for field, _ in js.TARGET_SETTINGS:
        fields[field] = {"column": f"t.{field}", "only": "true", "target": True,
                         "candidates": js._ranked(str(sample.get(field) or ""), sample, True, unity_catalog)}
    if with_id:
        fields["client_id"] = {"column": "t.client_id", "only": HAS_ID, "target": False,
                               "candidates": js._ranked(str(id_sample["client_id"]), id_sample, False)}
    for name, value in attrs.items():
        literal = "'" + str(name).replace("'", "''") + "'"
        fields[f"attr:{name}"] = {"column": f"(coalesce(t.client_attrs, '{{}}')::jsonb ->> {literal})",
                                  "only": "true", "target": False,
                                  "candidates": js._ranked("" if value is None else str(value), sample, False)}

    def check(field: str, candidate: str, p: Params) -> str:
        f = fields[field]
        return (f"coalesce(bool_and(case when {f['only']} then {f['column']} is not distinct from "
                f"{render_sql(candidate, p, target=f['target'], unity_catalog=unity_catalog)} "
                "else true end), true)")

    p = Params(groups=groups)
    keys = p.add(list(attrs))
    cols = [f"{check(field, f['candidates'][0], p)} as c{i}" for i, (field, f) in enumerate(fields.items())]
    # The same identity columns in the same order, as job_settings._identity compares them (json keeps order).
    cols.append(f"coalesce(bool_and((select coalesce(array_agg(k), '{{}}') from json_object_keys("
                f"coalesce(t.client_attrs, '{{}}')::json) k) = {keys}::text[]), true) as same_columns")
    first = fetch(f"select {', '.join(cols)} from {S}.control t where {WHERE}", p)[0]
    found: Dict[str, Optional[str]] = {}
    for i, (field, f) in enumerate(fields.items()):
        found[field] = f["candidates"][0] if first[f"c{i}"] else None
        rest = [] if found[field] is not None else f["candidates"][1:]
        # Every candidate the reference would try, in its order, a batch per scan.
        for at in range(0, len(rest), CANDIDATES_PER_SCAN):
            batch, p2 = rest[at:at + CANDIDATES_PER_SCAN], Params(groups=groups)
            ok = fetch(f"select {', '.join(f'{check(field, c, p2)} as k{n}' for n, c in enumerate(batch))} "
                       f"from {S}.control t where {WHERE}", p2)[0]
            hit = next((c for n, c in enumerate(batch) if ok[f"k{n}"]), None)
            if hit is not None:
                found[field] = hit
                break

    # A mixed setting lists its 20 most common values (the reference lists them all): enough to say what differs.
    def values_of(column: str, only: str = "true") -> dict:
        rows = fetch(f"select {column} as v, count(*) as n from {S}.control t where {WHERE} and {only} "
                     f"group by 1 order by 2 desc limit 20", {"groups": groups})
        return {"mixed": {("" if r["v"] is None else str(r["v"])): r["n"] for r in rows}}

    for field, _ in js.TARGET_SETTINGS:
        out[field] = {"value": found[field]} if found[field] is not None else values_of(f"t.{field}")
    if with_id:
        out["client_id"] = {"value": found["client_id"]} if found["client_id"] is not None else \
            values_of("t.client_id", HAS_ID)
    else:
        out["client_id"] = {"value": None}
    if not first["same_columns"]:
        out["identity"] = values_of("coalesce((select string_agg(k, ', ' order by k) from "
                                    "jsonb_object_keys(coalesce(t.client_attrs, '{}')::jsonb) k), '')")
    elif any(found[f"attr:{name}"] is None for name in attrs):
        bad = next(name for name in attrs if found[f"attr:{name}"] is None)
        out["identity"] = {"mixed": {bad: n}}
    else:
        out["identity"] = {"value": {name: found[f"attr:{name}"] for name in attrs}}
    return {k: out[k] for k in ("fan_in", "write_mode_changes", "write_mode_full", "freshness_sla_minutes",
                                "unity_catalog", "sink_catalog", "sink_schema", "sink_table", "client_id",
                                "identity")}


def _new_values(settings: dict, now: dict, p: Params) -> Dict[str, str]:
    """{column: SQL of its new value} for the columns a settings edit sets, as job_settings._desired does."""
    new: Dict[str, str] = {}
    current = lambda name: now.get(name, {}).get("value")
    if "fan_in" in settings or "client_id" in settings:
        filled = render_sql(settings.get("client_id") or current("client_id")
                            or js.default_client_id(current("fan_in")), p)
        if "fan_in" in settings:
            new["client_id"] = filled if settings["fan_in"] else "null::text"
        else:
            new["client_id"] = f"(case when {HAS_ID} then {filled} end)"
    branches = []
    normal = lambda mode: q.normalize_value("write_mode", mode)
    if "write_mode_changes" in settings:
        branches.append(f"when t.load_type = 'ct' then {p.add(normal(settings['write_mode_changes']))}::text")
    if "write_mode_full" in settings:
        branches.append(f"when t.load_type = 'full' then {p.add(normal(settings['write_mode_full']))}::text")
    if branches:
        new["write_mode"] = f"(case {' '.join(branches)} else t.write_mode end)"
    if "freshness_sla_minutes" in settings:
        value = settings["freshness_sla_minutes"]
        new["freshness_sla_minutes"] = f"{p.add(None if value in (None, '') else int(value))}::int"
    for field, _ in js.TARGET_SETTINGS:
        template = settings.get(field)
        if template:
            new[field] = render_sql(template, p, target=True, unity_catalog=bool(current("unity_catalog")))
    if "identity" in settings:
        new["client_attrs"] = _attrs_sql(settings["identity"] or {}, p)
    return new


def _differs(column: str, expr: str) -> str:
    """Whether a row's column changes, compared as queries.plan_edit compares it (JSON as objects, names
    without regard to case)."""
    if column == "client_attrs":
        return f"(t.client_attrs::jsonb is distinct from ({expr})::jsonb)"
    if column in q.CASE_INSENSITIVE:
        return f"(lower(t.{column}) is distinct from lower({expr}))"
    return f"(t.{column} is distinct from {expr})"


def _shape(settings: dict, now: dict, groups: List[str]):
    """(select list of each row's new values and flags, params, columns set). Flags: changed, reseed, bad_id,
    mode_without_target."""
    p = Params(groups=groups)
    new = _new_values(settings, now, p)
    if not new:
        return None, p, []
    changed = " or ".join(_differs(c, e) for c, e in new.items())
    target_moves = " or ".join(_differs(c, new[c]) for c in ("sink_catalog", "sink_schema", "sink_table", "client_id")
                               if c in new) or "false"
    sink_moves = " or ".join(_differs(c, new[c]) for c in ("sink_catalog", "sink_schema", "sink_table") if c in new) or "false"
    mode = _differs("write_mode", new["write_mode"]) if "write_mode" in new else "false"
    client = new.get("client_id", "t.client_id")
    sink = " || '.' || ".join(new.get(c, f"t.{c}") for c in ("sink_catalog", "sink_schema", "sink_table"))
    select = (f"t.id, t.task_collection, t.src_database, t.src_schema, t.src_table, t.load_type, "
              + ", ".join(f"{e} as n_{c}" for c, e in new.items())
              + f", ({changed}) as changed, (({target_moves}) and t.load_type in ('ct', 'cursor')) as reseed"
              f", ({client} is not null and {client} !~ {p.add(CLIENT_ID_RE)}) as bad_id"
              f", ({mode} and not ({sink_moves})) as mode_without_target"
              f", lower({sink}) as n_sink, ({client} is null) as no_id, {client} as n_client"
              # An identity column may not share a name with one of the table's own columns (queries.client_attrs_errors).
              f", (select min(btrim(c)) from unnest(string_to_array(lower(coalesce(t.select_list, '')), ',')) c "
              f"where btrim(c) = any({p.add([str(n).lower() for n in (settings.get('identity') or {})])}::text[])) as attr_clash")
    return select, p, list(new)


def preview(fetch: Callable, groups: List[str], settings: dict, now: dict) -> dict:
    """{summary, errors} of a settings edit without writing: how many tables change, how many load again, and what
    would refuse it (template errors, a client_id that breaks the rules, a write mode changed without new target
    names, two tables on one target without fan-in)."""
    fan_in = settings.get("fan_in", now.get("fan_in", {}).get("value"))
    errors = js.check_templates(settings, fan_in=bool(fan_in))
    n = fetch(f"select count(*) as n from {S}.control t where {WHERE}", {"groups": groups})[0]["n"]
    summary = {"rows": n, "changed": 0, "reseed": 0, "errors": 0, "applied": False}
    if errors:
        return {"summary": summary, "errors": errors}
    select, p, _ = _shape(settings, now, groups)
    if select is None:
        return {"summary": summary, "errors": []}
    counts = fetch(f"""select count(*) filter (where changed) as changed,
        count(*) filter (where changed and reseed) as reseed, count(*) filter (where changed and bad_id) as bad_id,
        count(*) filter (where changed and mode_without_target) as mode,
        min(case when changed and bad_id then n_client end) as example,
        count(*) filter (where attr_clash is not null) as clash, min(attr_clash) as clash_example
        from (select {select} from {S}.control t where {WHERE}) x""", p)[0]
    summary.update(changed=counts["changed"], reseed=counts["reseed"])
    if counts["bad_id"]:
        errors.append(f"{counts['bad_id']} tables would get a client_id that is not allowed (e.g. {counts['example']}): "
                      "letters, digits and _ . : - only, 1 to 128 characters")
    if counts["clash"]:
        errors.append(f"{counts['clash']} tables already have a column named like an identity column (e.g. "
                      f"{counts['clash_example']}): give the identity column another name")
    if counts["mode"]:
        errors.append(f"{counts['mode']} tables would change write mode on the same target: a change log and the "
                      "current rows can't share a table, so change the target names too")
    # Two tables can share a target only among tables left without a client_id: none when every table keeps one.
    keeps_ids = now.get("fan_in", {}).get("value") is True and settings.get("fan_in", True) is not False
    clash = {"n": 0} if keeps_ids else fetch(f"""select count(*) as n, min(n_sink) as example from (select n_sink from (select {select}
        from {S}.control t where {WHERE}) x where no_id group by n_sink having count(*) > 1) y""", p)[0]
    if clash["n"]:
        errors.append(f"{clash['n']} targets would be written by more than one table (e.g. {clash['example']}): "
                      "add {database} to the target names, or turn fan-in on")
    summary["errors"] = len(errors)
    return {"summary": summary, "errors": errors}


def apply(cur, groups: List[str], settings: dict, now: dict, user: str, batch: str) -> dict:
    """Write a settings edit in one statement inside the caller's transaction (which holds the job's and groups'
    locks): the changed rows, a reseed of the change-tracked and cursor tables that get a new target or client_id,
    and one change-log line per database (its group, the columns set and the edit; no control id, since one line
    stands for every table of the database)."""
    select, p, columns = _shape(settings, now, groups)
    if select is None:
        return {"changed": 0, "reseed": 0, "logged": 0}
    sets = ", ".join(f"{c} = x.n_{c}" for c in columns)
    detail, reason = p.add("reseed after edit: sink_changed"), p.add("edit:sink_changed")
    note = p.add(f"job settings: {json.dumps(settings, sort_keys=True)}")
    cur.execute(f"""
with x as (select {select} from {S}.control t where {WHERE}),
upd as (update {S}.control c set {sets} from x where c.id = x.id and x.changed
        returning c.id, x.task_collection, x.src_database, x.src_schema, x.src_table, x.reseed),
flip as (update {S}.checkpoint set {q._RESEED_SET}, detail = {detail}
         where control_id in (select id from upd where reseed) returning control_id),
queued as (insert into {S}.reseed_queue (control_id, src_database, src_schema, src_table, reason)
           select id, src_database, src_schema, src_table, {reason} from upd where reseed returning control_id),
logged as (insert into {S}.control_change (changed_by, action, control_id, task_collection, field, old_value,
               new_value, reseed_reason, batch_id)
           select {p.add(user or None)}, 'edit', null, task_collection, {p.add(', '.join(columns))}, null,
                  coalesce(src_database, '') || ': ' || count(*) || ' tables; ' || {note},
                  case when bool_or(reseed) then 'sink_changed' end, {p.add(batch)}
           from upd group by task_collection, src_database returning 1)
select (select count(*) from upd) as changed, (select count(*) from upd where reseed) as reseed,
       (select count(*) from logged) as logged, (select count(*) from flip) as flipped,
       (select count(*) from queued) as queued""", p)
    row = cur.fetchone()
    return dict(row) if isinstance(row, dict) else dict(zip([d.name for d in cur.description], row))


def mixed_sinks(fetch: Callable, groups: List[str]) -> List[str]:
    """After an edit: the job's targets claimed both by a dedicated row and by other rows (lakefed stops every
    table on such a target), anywhere in control."""
    rows = fetch(f"""select lower(c.sink_catalog || '.' || c.sink_schema || '.' || c.sink_table) as sink
        from {S}.control c where lower(c.sink_catalog || '.' || c.sink_schema || '.' || c.sink_table) in
            (select distinct lower(t.sink_catalog || '.' || t.sink_schema || '.' || t.sink_table)
             from {S}.control t where {WHERE})
        group by 1 having count(*) filter (where c.client_id is null) > 0 and count(*) > 1 limit 5""",
                 {"groups": groups})
    return [r["sink"] for r in rows]
