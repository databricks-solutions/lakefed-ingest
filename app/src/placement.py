"""Which group each database of a job goes to. A server gets one group for about every tables-per-group of its tables
(the job's own, or TABLES_PER_GROUP), with no group of its own for a small remainder, or a number of groups an older
caller sets. Its databases are spread
so each group has about the same number of tables: the biggest first, each into the group with the fewest tables so
far. A database is never split: all its tables are in one group. Databases added to a job later each join their
server's group with the fewest tables at that time, and nothing already placed moves. Pure, so it is tested without a
database."""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple, Union

TABLES_PER_GROUP = 10_000
# A remainder of up to this share of a group joins the other groups instead of making a group of its own: 100,200
# tables make 10 groups, not 11, and 101,500 make 11.
REMAINDER_SHARE = 0.1


def groups_for_tables(tables: int, per_group: int = TABLES_PER_GROUP) -> int:
    """The default number of groups for a server's tables: one per per_group tables, a remainder of more than a tenth
    of a group adding one more, and at least one."""
    whole, remainder = divmod(max(0, int(tables)), per_group)
    return max(1, whole + (1 if remainder > per_group * REMAINDER_SHARE else 0))


@dataclass(frozen=True)
class TablesPerGroup:
    """A job's group size: each server gets one group for about this many of its tables (groups_for_tables)."""
    tables: int


# How many groups a server gets: a number per server (an older caller's groups_per_server), a TablesPerGroup, or None
# for the default size.
Groups = Union[int, TablesPerGroup, None]


def group_count(tables: int, groups: Groups = None) -> int:
    """A server's number of groups for its tables under ``groups``."""
    if isinstance(groups, TablesPerGroup):
        return groups_for_tables(tables, groups.tables)
    return int(groups) if groups else groups_for_tables(tables)


def sizing(groups_per_server: Optional[int], tables_per_group: Optional[int], job_tables_per_group=None) -> Groups:
    """What a request's grouping is: a number of groups it names, else the tables per group it names, else the job's
    own (kept in its options), else None (the default size)."""
    if groups_per_server:
        return int(groups_per_server)
    size = tables_per_group or job_tables_per_group
    return TablesPerGroup(int(size)) if size else None


def even(databases: Iterable[Tuple[str, int]], groups: int) -> Dict[str, int]:
    """{database name, lower case: its group's index, from 0}, for a new job's databases of one server, given as
    (name, tables). The biggest database goes first, each into the group with the fewest tables so far, the lowest
    index on a tie, so the same databases always land the same way (a resumed Create places them again identically)."""
    sizes: Dict[str, int] = {}
    for name, tables in databases:
        key = str(name or "").lower()
        sizes[key] = sizes.get(key, 0) + int(tables or 0)
    loads = [0] * max(1, int(groups))
    out: Dict[str, int] = {}
    for name, tables in sorted(sizes.items(), key=lambda item: (-item[1], item[0])):
        index = min(range(len(loads)), key=lambda i: (loads[i], i))
        out[name] = index
        loads[index] += tables
    return out


def server_placements(rows: Iterable[Tuple[str, str, int]], groups: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    """{server: even placement of its databases}, from (server, database, tables) rows and {server: its groups}."""
    by_server: Dict[str, List[Tuple[str, int]]] = {}
    for server, database, tables in rows:
        by_server.setdefault(server, []).append((database, tables))
    return {server: even(databases, groups.get(server, 1)) for server, databases in by_server.items()}


class Lightest:
    """Places the databases added to an existing job: each new one joins its server's group with the fewest tables now
    (the first by name on a tie), and every table of it counts there, so the next new database sees it. place() is
    called once per table, as a plan reads its lines; a database already placed keeps its group."""

    def __init__(self, tables_by_group: Dict[str, Dict[str, int]]):
        self._loads = {server: dict(groups) for server, groups in tables_by_group.items()}
        self._placed: Dict[Tuple[str, str], str] = {}

    def knows(self, server: str) -> bool:
        return bool(self._loads.get(server))

    def place(self, server: str, database: str, tables: int = 1) -> Optional[str]:
        """The group of a new database's tables (one table each call by default), or None when the server has no group
        in the job yet."""
        loads = self._loads.get(server)
        if not loads:
            return None
        key = (server, str(database or "").lower())
        group = self._placed.get(key)
        if group is None:
            group = min(sorted(loads), key=lambda name: loads[name])
            self._placed[key] = group
        loads[group] += tables
        return group


def _group_number(name: str) -> Optional[int]:
    tail = str(name).rsplit("_c", 1)
    return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else None


def regroup(prefix: str, databases: Dict[str, Tuple[str, int]], groups: int) -> Dict[str, object]:
    """A server's databases spread again over a new number of groups, for a job whose number of groups changes, moving
    as few as it can. databases is {database, lower case: (its group now, its tables)}; prefix names a new group
    (prefix + "_c03"). With more groups, every group stays and new ones are added; with fewer, the groups with the most
    tables stay. A group that stays keeps its databases, biggest first, while it holds no more than an even share of
    the tables; the rest, and every database of a group that goes, are placed biggest first into the group with the
    fewest tables. A new group takes the lowest number no group uses. Returns {"group_of": {database: its group after},
    "moves": {database: (before, after)}, "groups": the groups after, "new": the groups made, "emptied": the groups
    left with no database}."""
    count = max(1, int(groups))
    loads_now: Dict[str, int] = {}
    members: Dict[str, List[Tuple[str, int]]] = {}
    for name, (group, tables) in databases.items():
        loads_now[group] = loads_now.get(group, 0) + int(tables)
        members.setdefault(group, []).append((name, int(tables)))
    stay = sorted(sorted(loads_now, key=lambda g: (-loads_now[g], g))[:count])
    used = {number for number in (_group_number(g) for g in loads_now) if number is not None}
    made, number = [], 1
    while len(stay) + len(made) < count:
        while number in used:
            number += 1
        made.append(f"{prefix}_c{number:02d}")
        used.add(number)
    after = sorted(stay + made)
    share = sum(loads_now.values()) / count
    loads = {group: 0 for group in after}
    group_of: Dict[str, str] = {}
    pool: List[Tuple[str, int]] = []
    for group in loads_now:
        for name, tables in sorted(members[group], key=lambda item: (-item[1], item[0])):
            if group in loads and (loads[group] == 0 or loads[group] + tables <= share):
                group_of[name] = group
                loads[group] += tables
            else:
                pool.append((name, tables))
    for name, tables in sorted(pool, key=lambda item: (-item[1], item[0])):
        group = min(after, key=lambda g: (loads[g], g))
        group_of[name] = group
        loads[group] += tables
    return {"group_of": group_of, "groups": after, "new": made, "emptied": sorted(set(loads_now) - set(after)),
            "moves": {name: (databases[name][0], group) for name, group in group_of.items() if group != databases[name][0]}}
