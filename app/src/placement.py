"""Which group each database of a job goes to. A server gets one group for about every tables-per-group of its tables
(the job's own, or TABLES_PER_GROUP), with no group of its own for a small remainder, or a number of groups an older
caller sets. Its databases are spread so each group has about the same number of tables: the biggest first, each into
the group with the fewest tables so far.

A database that fits in a group goes whole into one group. A database with more tables than a group holds (by the same
rule: more than the tables per group, plus a small remainder) is spread over as many groups as it needs: the groups with
the fewest tables so far. Its tables, in name order, are cut into parts sized to the room in those groups (room_shares):
the lightest group takes the biggest part, so the groups end as level as the rule allows. A group already above the
level takes no part, so a database can end in fewer groups than it needed. Each group then checks the database for
changes to its own tables and loads only those, which costs more queries and connections on its server
(tests/split_database_test.py proves the loads stay right). A job sized by a number of groups (an older caller) keeps
every database whole.

Databases are placed biggest first. Databases added to a job later each join their server's group with the fewest tables
at that time (a database too big for one group, the groups with the fewest), and nothing already placed moves. A regroup
moves each part of a split database as a unit of its own. Pure, so it is tested without a database."""
from bisect import bisect_right
from dataclasses import dataclass
from itertools import accumulate
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

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


def split_size(groups: Groups = None) -> Optional[int]:
    """The tables per group a database is measured against to decide whether it is spread over several groups: the
    job's own size, or TABLES_PER_GROUP by default. None for a job sized by a number of groups (an older caller's
    groups_per_server), whose databases always stay whole."""
    if isinstance(groups, TablesPerGroup):
        return int(groups.tables)
    return None if groups else TABLES_PER_GROUP


def parts_for(tables: int, per_group: Optional[int], groups: int) -> int:
    """How many groups a database's tables go to: one while it fits in a group (groups_for_tables' rule, a small
    remainder included), else as many as its tables need, never more than the server has groups."""
    if not per_group:
        return 1
    return max(1, min(int(groups), groups_for_tables(tables, per_group)))


def room_shares(loads: List[int], tables: int) -> List[int]:
    """How many of ``tables`` new tables each group takes, given the tables the groups hold now (``loads``): the
    lightest groups are filled up first until they reach the next group's level, then those rise together, so the
    groups end as level as whole tables allow. A group already above that level takes none. A table left over when the
    groups are level goes to the group that held the fewest (the lowest position on a tie). The shares are in the order
    of ``loads``."""
    order = sorted(range(len(loads)), key=lambda at: (loads[at], at))
    remaining, active, level = int(tables), 1, loads[order[0]]
    while active < len(order):
        cost = active * (loads[order[active]] - level)
        if remaining < cost:
            break
        remaining -= cost
        level = loads[order[active]]
        active += 1
    each, extra = divmod(remaining, active)
    shares = [0] * len(loads)
    for rank, at in enumerate(order[:active]):
        shares[at] = level - loads[at] + each + (1 if rank < extra else 0)
    return shares


@dataclass(frozen=True)
class Spread:
    """A database spread over several groups: the ``groups`` (indexes in a placement, names once a job names them), in
    the order its tables are cut, and how many of its tables each one holds (``shares``)."""
    groups: Tuple
    shares: Tuple[int, ...]

    def group_for(self, ordinal: int, tables: int):
        """The group of the table at ``ordinal`` (its place among the database's ``tables`` tables in name order, from
        0). Each group takes a run of tables as long as its share. When the database has other tables than it was
        placed for, the runs keep their proportions."""
        position = int(ordinal) * sum(self.shares) // max(1, int(tables))
        return self.groups[min(len(self.groups) - 1, bisect_right(list(accumulate(self.shares)), position))]


def as_spread(placed: Union[str, Spread]) -> Spread:
    """A database's group (a name) or its Spread as a Spread, so one that goes whole is read as the others are."""
    return placed if isinstance(placed, Spread) else Spread(groups=(placed,), shares=(1,))


def _spread_over(groups: Iterable, shares: Iterable[int]) -> Spread:
    """A database's groups with the tables each takes, leaving out the groups that take none (all of them, for a
    database with no tables, so there is always a group)."""
    pairs = list(zip(groups, shares))
    taken = [(group, share) for group, share in pairs if share > 0] or pairs[:1]
    return Spread(groups=tuple(group for group, _ in taken), shares=tuple(share for _, share in taken))


@dataclass(frozen=True)
class Within:
    """A database a job holds in several groups already (it was split): each of its new tables goes to the lightest of
    these ``groups`` (Lightest.place_within)."""
    groups: Tuple[str, ...]


# Where a database goes: one group's index, or for a spread database its groups' indexes and shares, its first part first.
Place = Union[int, Spread]


def indexes(place: Place) -> Tuple[int, ...]:
    """A placement's group indexes, one for a database that goes whole."""
    return tuple(place.groups) if isinstance(place, Spread) else (int(place),)


def named(place: Place, name_of: Callable[[int], str]) -> Union[str, Spread]:
    """A placement with its group indexes turned into the groups' names: one name for a database that goes whole."""
    if isinstance(place, Spread):
        return Spread(groups=tuple(name_of(index) for index in place.groups), shares=place.shares)
    return name_of(int(place))


def even(databases: Iterable[Tuple[str, int]], groups: int, per_group: Optional[int] = None) -> Dict[str, Place]:
    """{database name, lower case: where it goes}, for a new job's databases of one server, given as (name, tables). The
    biggest database goes first, into the group with the fewest tables so far, the lowest index on a tie, so the same
    databases always land the same way (a resumed Create places them again identically). With ``per_group``
    (split_size), a database with more tables than a group holds goes to the groups with the fewest tables instead, as
    many as it needs (parts_for), its tables shared out by the room in each (room_shares)."""
    sizes: Dict[str, int] = {}
    for name, tables in databases:
        key = str(name or "").lower()
        sizes[key] = sizes.get(key, 0) + int(tables or 0)
    loads = [0] * max(1, int(groups))
    out: Dict[str, Place] = {}
    for name, tables in sorted(sizes.items(), key=lambda item: (-item[1], item[0])):
        parts = parts_for(tables, per_group, len(loads))
        lightest = sorted(sorted(range(len(loads)), key=lambda i: (loads[i], i))[:parts])
        shares = room_shares([loads[i] for i in lightest], tables)
        for index, share in zip(lightest, shares):
            loads[index] += share
        spread = _spread_over(lightest, shares)
        out[name] = spread.groups[0] if len(spread.groups) == 1 else spread
    return out


def server_placements(rows: Iterable[Tuple[str, str, int]], groups: Dict[str, int],
                      per_group: Optional[int] = None) -> Dict[str, Dict[str, Place]]:
    """{server: even placement of its databases}, from (server, database, tables) rows and {server: its groups}."""
    by_server: Dict[str, List[Tuple[str, int]]] = {}
    for server, database, tables in rows:
        by_server.setdefault(server, []).append((database, tables))
    return {server: even(databases, groups.get(server, 1), per_group) for server, databases in by_server.items()}


class Lightest:
    """Places the databases added to an existing job: each new one joins its server's group with the fewest tables now
    (the first by name on a tie), and every table of it counts there, so the next new database sees it. place() is
    called once per table, as a plan reads its lines; a database already placed keeps its group. A database with more
    tables than a group holds (per_group, split_size) is placed once by spread() instead, over the groups with the
    fewest tables, sized to the room in each."""

    def __init__(self, tables_by_group: Dict[str, Dict[str, int]], per_group: Optional[int] = None):
        self._loads = {server: dict(groups) for server, groups in tables_by_group.items()}
        self._placed: Dict[Tuple[str, str], str] = {}
        self._spread: Dict[Tuple[str, str], Spread] = {}
        self._tables: Dict[Tuple[str, str, tuple], str] = {}
        self.per_group = per_group

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

    def hold(self, server: str, database: str, table: tuple, group: str) -> None:
        """Note that the job holds this table (its name in the database, lower case) in this group: a table of a split
        database it is asked about again keeps its group and adds no load, since the group's load counts it already."""
        self._tables[(server, str(database or "").lower(), table)] = group

    def place_within(self, server: str, database: str, groups: Iterable[str], table: tuple) -> str:
        """The group of a table added to a database held in several ``groups``: the table's own group if the job holds
        it, else the lightest of those groups now (the first by name on a tie), which then counts the table."""
        key = (server, str(database or "").lower(), table)
        if key not in self._tables:
            loads = self._loads.get(server) or {}
            self._tables[key] = min(groups, key=lambda name: (loads.get(name, 0), name))
            loads[self._tables[key]] = loads.get(self._tables[key], 0) + 1
        return self._tables[key]

    def parts(self, server: str, tables: int) -> int:
        """How many of the server's groups a new database with ``tables`` tables needs (parts_for); 1 when it fits."""
        return parts_for(tables, self.per_group, len(self._loads.get(server) or {}) or 1)

    def spread(self, server: str, database: str, tables: int) -> Optional[Spread]:
        """The groups of a new database with ``tables`` tables, its first part first: the server's groups with the
        fewest tables now, as many as it needs (parts_for), each taking the part it has room for (room_shares) and
        counting it. A database that fits is in one group, as place() gives it. None when the server has no group in
        the job yet. A database spread already keeps its groups."""
        loads = self._loads.get(server)
        if not loads:
            return None
        key = (server, str(database or "").lower())
        if key not in self._spread:
            lightest = sorted(loads, key=lambda name: (loads[name], name))
            chosen = sorted(lightest[:parts_for(tables, self.per_group, len(loads))])
            shares = room_shares([loads[group] for group in chosen], tables)
            for group, share in zip(chosen, shares):
                loads[group] += share
            self._spread[key] = _spread_over(chosen, shares)
        return self._spread[key]


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
