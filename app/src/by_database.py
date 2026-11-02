"""A big job written one database at a time: how many groups a server's tables take, the pages of databases a source is
read in, the units a selection takes, and the plan of one database. Pure: the endpoints and the background task read
Lakebase and write each database's own transaction (main.py)."""
import contextlib
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import new_job as nj
import placement

# A group of a big job takes about this many tables, so a server with 5.3 million tables gets about 425 groups.
TABLES_PER_GROUP = placement.TABLES_PER_GROUP
# Databases are read this many at a time, each page starting after the key of the last one read.
DATABASE_PAGE = 500
# A transaction of a big job writes its databases up to about this many tables together. A database is never split
# across transactions, so one bigger than this has a transaction of its own.
TABLES_PER_TRANSACTION = 5_000
# The check reads the tables of this many databases in one statement.
DATABASES_PER_READ = 100
# A selection of at most this many tables keeps the plan of each database from its check, so the write takes those plans
# instead of reading and planning the databases again. Above it the plans are not held, and the write reads them again.
PLAN_ONCE_TABLES = 200_000


def _now() -> float:
    return time.monotonic()


class Stages:
    """Where a big write's time goes, in seconds, for its task's progress: "check" is the check pass (from the start until
    the write pass begins), "write" is the write pass so far, and "read" and "plan" are the reads and the planning of both
    passes so far. Each value is cumulative and rounded to a tenth of a second, so it only grows as the write goes on."""

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or _now
        self._started = self._clock()
        self._write_started: Optional[float] = None
        self._spent = {"read": 0.0, "plan": 0.0}

    @contextlib.contextmanager
    def timed(self, stage: str) -> Iterator[None]:
        """Adds the time the block takes to the read or plan total."""
        started = self._clock()
        try:
            yield
        finally:
            self._spent[stage] += self._clock() - started

    def begin_write(self) -> None:
        self._write_started = self._clock()

    def seconds(self) -> Dict[str, float]:
        """The four stages so far, for a task's progress."""
        now = self._clock()
        if self._write_started is None:
            check, write = now - self._started, 0.0
        else:
            check, write = self._write_started - self._started, now - self._write_started
        return {"check": round(check, 1), "read": round(self._spent["read"], 1),
                "plan": round(self._spent["plan"], 1), "write": round(write, 1)}


def groups_for_tables(tables: int) -> int:
    """How many groups a server's tables take by default: one per TABLES_PER_GROUP tables, no group for a remainder of
    a tenth of a group or less, at least one (placement.groups_for_tables)."""
    return placement.groups_for_tables(tables)


def groups_by_server(servers: List[dict], groups_per_server: placement.Groups = None) -> Dict[str, int]:
    """{server: its groups} for a selection's servers (queries.selected_servers_sql rows), under the job's grouping
    (placement.group_count: its tables per group, or a number of groups an older caller sets)."""
    return {s["server"]: placement.group_count(s["tables"], groups_per_server) for s in servers}


def group_of_placement(job_name: str, placements: Dict[str, Dict[str, int]]) -> Callable[[str, str], str]:
    """Where a new job's database goes, from its even placement (placement.server_placements over the selection's
    databases): its server's group with that index."""
    def group_of(server: str, database: str) -> str:
        index = placements.get(server, {}).get(str(database or "").lower(), 0)
        return f"{nj.q.identifier(job_name)}_{nj.q.identifier(server)}_c{index + 1:02d}"
    return group_of


def database_pages(page: Callable[[Tuple[str, str, str]], List[dict]]) -> Iterator[dict]:
    """Every database of a source in key order, a page at a time. page(after) returns up to DATABASE_PAGE rows after the
    key (server, lower-case name, name) of the last one read; the first page is asked with ('', '', '')."""
    after = ("", "", "")
    while True:
        rows = page(after)
        yield from rows
        if len(rows) < DATABASE_PAGE:
            return
        last = rows[-1]
        after = (last["server"], str(last["src_database"]).lower(), last["src_database"])


class Unit:
    """One database a selection takes: all its tables (every is True) or only the named ones (schema.table, lower case).
    exact is True when the server and database are the names the source listing returned, so a read can match them
    as stored; a name the user typed may differ in case and is matched without regard to case."""
    __slots__ = ("server", "database", "every", "tables", "exact")

    def __init__(self, server: str, database: str, every: bool, tables: Optional[frozenset] = None,
                 exact: bool = False):
        self.server, self.database, self.every, self.tables, self.exact = server, database, every, tables, exact

    def keeps(self, row: dict) -> bool:
        """Whether a table row of this database is one the selection takes."""
        if self.every:
            return True
        return f"{row['src_schema']}.{row['src_table']}".lower() in self.tables


def selection_units(selection, server_database_keys: Iterator[Tuple[str, str]]) -> Iterator[Unit]:
    """The databases a selection takes, in order: each database the whole_server filter matches (given as
    server_database_keys, from database_pages), then the databases named and the tables named, skipping those the
    whole server already covers (a database whose name the filter keeps) and naming each database once."""
    seen = set()
    for server, database in server_database_keys:
        seen.add((server.lower(), database.lower()))
        yield Unit(server, database, True, exact=True)
    filter_ = str(selection.filter or "").lower()
    covered = lambda database: bool(selection.whole_server) and filter_ in database.lower()
    for named in selection.databases:
        key = (named.server.lower(), named.database.lower())
        if key in seen or covered(named.database):
            continue
        seen.add(key)
        yield Unit(named.server, named.database, True)
    by_database: Dict[tuple, set] = {}
    for named in selection.tables:
        for name in named.tables:
            by_database.setdefault((named.server.lower(), named.database.lower(), named.database), set()).add(name.lower())
    for (server, database_lower, database), names in by_database.items():
        if (server, database_lower) in seen or covered(database):
            continue
        seen.add((server, database_lower))
        yield Unit(server, database, False, frozenset(names))


def read_key(server: str, database: str) -> str:
    """A database as database_rows_chunk_sql matches it: its lower-case server and name, joined by the unit
    separator."""
    return f"{str(server).lower()}\x1f{str(database).lower()}"


def read_batches(units: List[Unit]) -> Iterator[List[Unit]]:
    """The units in order, DATABASES_PER_READ at a time, so one statement reads a batch's tables."""
    for at in range(0, len(units), DATABASES_PER_READ):
        yield units[at:at + DATABASES_PER_READ]


def rows_by_unit(units: List[Unit], rows: List[dict]) -> List[Tuple[Unit, List[dict]]]:
    """Each unit with the table rows of its database that it takes, out of the rows of all the units' databases (read
    together, in one statement)."""
    by_key: Dict[str, List[dict]] = {}
    for row in rows:
        by_key.setdefault(read_key(row["server"], row["src_database"]), []).append(row)
    return [(unit, [row for row in by_key.get(read_key(unit.server, unit.database), []) if unit.keeps(row)])
            for unit in units]


class Checked:
    """A database the check planned: its unit, its table count, and its plan. The plan is None when the selection is
    above PLAN_ONCE_TABLES, and the write reads and plans the database again."""
    __slots__ = ("unit", "tables", "plan")

    def __init__(self, unit: Unit, tables: int, plan: Optional[dict] = None):
        self.unit, self.tables, self.plan = unit, tables, plan


def keeps_plans(tables: int) -> bool:
    """Whether a selection of this many tables keeps each database's plan from the check (see PLAN_ONCE_TABLES)."""
    return int(tables) <= PLAN_ONCE_TABLES


def transactions_of(checked: List[Checked]) -> Iterator[List[Checked]]:
    """The checked databases, in order, packed into transactions: each is added to the transaction being filled until
    adding it would take that past TABLES_PER_TRANSACTION, and then the next transaction starts."""
    chunk: List[Checked] = []
    tables = 0
    for database in checked:
        if chunk and tables + database.tables > TABLES_PER_TRANSACTION:
            yield chunk
            chunk, tables = [], 0
        chunk.append(database)
        tables += database.tables
    if chunk:
        yield chunk


def plan_database(job_name: str, given: dict, rows: List[dict], columns: List[str], groups: Dict[str, int],
                  taken: set, own: set, group_of: Optional[Callable[[str, str], str]] = None,
                  have: Optional[set] = None, join=None, new_job: bool = False, unity_catalog: bool = True) -> dict:
    """The plan of one database's rows (each {server, src_database, src_schema, src_table, line, fields}), made as
    new_job.plan makes a file's: its lines, groups and errors. groups is {server: its groups} (groups_by_server) and
    taken the groups other jobs use; the job's own groups (own) are not taken. A new job places its databases by
    groups; an existing job passes its own group_of and have (new_job.job_inputs). new_job is the job being created
    (new_job.plan's flag), and unity_catalog its naming rule (job_settings.unity_catalog_of). Each line's number is its
    line in the uploaded file, or None for a discovery."""
    text = nj.csv_of(columns, [row["fields"] for row in rows])
    if group_of is None:
        group_of = lambda server, database: nj.group_name(job_name, server, database, groups.get(server, 1))
    plan = nj.plan(job_name, text, given, taken_groups=set(taken) - set(own), group_of=group_of, have=have, join=join,
                   new_job=new_job, unity_catalog=unity_catalog)
    for line, row in zip(plan["lines"], rows):
        line["line"] = row.get("line") or None
    return plan


def summary_of(job_name: str, servers: List[dict], default_load_type: str = "ct",
               groups_of: Optional[Dict[str, int]] = None) -> dict:
    """What a big job's preview shows before Create, from the selection's servers (queries.selected_servers_sql): each
    server's databases, tables, tables by load type (a line that sets none takes the job's default_load_type) and group
    names, and the totals: the same fields per server as new_job.summary. Nothing is checked yet: checked is False, and
    by_database is True, so the page says the lines are read when the write runs."""
    groups_of = groups_of or groups_by_server(servers)
    out_servers = []
    for s in sorted(servers, key=lambda s: s["server"]):
        names = nj.group_names(job_name, s["server"], groups_of[s["server"]])
        load_types = {load_type: int(s.get(f"load_{load_type}") or 0) for load_type in nj.LOAD_TYPES}
        load_types[default_load_type] = load_types.get(default_load_type, 0) + int(s.get("load_unset") or 0)
        out_servers.append({"server": s["server"], "databases": int(s["databases"]), "tables": int(s["tables"]),
                            "groups": names, "read_through": "JDBC" if ":" in s["server"] else "UC connection",
                            "load_types": {k: n for k, n in load_types.items() if n}})
    all_groups = [g for s in out_servers for g in s["groups"]]
    return {"servers": out_servers, "tables": sum(s["tables"] for s in servers),
            "databases": sum(s["databases"] for s in servers), "groups": all_groups, "in_job": 0,
            "new_groups": all_groups, "invalid": [], "by_database": True, "checked": False}
