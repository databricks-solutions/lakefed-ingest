"""Discovery's per-database checks and reads on the SQL warehouse (warehouse_reads.advance), with the Lakebase store and
the warehouse faked: what each step starts, saves and answers."""
import threading
import time

import pytest

import warehouse
import warehouse_reads as wr


class _Store:
    """The discovery rows of some discoveries in memory, with the store's calls, as main.WarehouseReadStore makes them
    in Lakebase. A claim is live for claim_s on the store's own clock (now), as claimed_at is; one lock stands for the
    server's transaction lock and the rows' locks. ``claims`` records each read claim: (server, cap, going before,
    claimed)."""

    def __init__(self, names=(), kind=wr.READ, discovery_id="d1", connection="sql_a"):
        self.rows, self.now, self.claims, self.finished = {}, 0.0, [], []
        self.lock = threading.RLock()
        if names:
            self.add(discovery_id, names, kind, connection)

    def add(self, discovery_id, names, kind=wr.READ, connection="sql_a"):
        status = "listed" if kind == wr.CHECK else "running"
        for n in names:
            self.rows[(discovery_id, n)] = {"status": status, "statement": None, "claimed_at": None, "open": None,
                                            "tables": None, "error": None, "why": None, "found": None,
                                            "connection": connection}

    def row(self, discovery_id, database):
        return self.rows[(discovery_id, database)]

    def _mine(self, discovery_id):
        return [(n, r) for (d, n), r in sorted(self.rows.items()) if d == discovery_id]

    def _work(self, kind, row):
        if kind == wr.CHECK:
            return row["open"] is None and row["status"] == "listed"
        return row["status"] == "running"

    def _live(self, row, claim_s=wr.CLAIM_S):
        return row["claimed_at"] is not None and self.now - row["claimed_at"] < claim_s

    def in_flight(self, kind, discovery_id):
        with self.lock:
            return [(n, r["statement"]) for n, r in self._mine(discovery_id) if self._work(kind, r) and r["statement"]]

    def claim(self, kind, discovery_id, first, limit, claim_s):
        with self.lock:
            free = [n for n, r in self._mine(discovery_id)
                    if self._work(kind, r) and not r["statement"] and not self._live(r, claim_s)]
            free.sort(key=lambda n: (n not in first, n))
            for n in free[:max(0, limit)]:
                self.row(discovery_id, n)["claimed_at"] = self.now
            return free[:max(0, limit)]

    def going(self, server, claim_s=wr.CLAIM_S):
        return sum(1 for r in self.rows.values() if r["connection"].lower() == server and self._work(wr.READ, r)
                   and self._live(r, claim_s))

    def claim_reads(self, server, discovery_ids, cap, limit, claim_s):
        with self.lock:
            going = self.going(server, claim_s)
            room = min(limit, cap - going)
            free = [(d, n) for d in discovery_ids for n, r in self._mine(d)
                    if r["connection"].lower() == server and self._work(wr.READ, r) and not r["statement"]
                    and not self._live(r, claim_s)][:max(0, room)]
            for d, n in free:
                self.row(d, n)["claimed_at"] = self.now
            self.claims.append((server, cap, going, len(free)))
            return free, going + len(free) >= cap

    def collect(self, discovery_id, claim_s, limit):
        with self.lock:
            found = [(n, r["statement"]) for n, r in self._mine(discovery_id)
                     if self._work(wr.READ, r) and r["statement"] and not self._live(r, claim_s)][:limit]
            for n, _ in found:
                self.row(discovery_id, n)["claimed_at"] = self.now
            return found

    def hold(self, reads):
        with self.lock:
            for d, n in reads:
                if self._work(wr.READ, self.row(d, n)):
                    self.row(d, n)["claimed_at"] = self.now

    def read_work(self, claim_s, max_age_s, draft_ttl_s):
        with self.lock:
            out = []
            for d in sorted({d for d, _ in self.rows}):
                mine = [r for _, r in self._mine(d) if self._work(wr.READ, r) and not self._live(r, claim_s)]
                waiting = sum(1 for r in mine if not r["statement"])
                if mine:
                    out.append({"discovery_id": d, "src_connection": self._mine(d)[0][1]["connection"],
                                "waiting": waiting, "orphaned": len(mine) - waiting})
            return out

    def started(self, kind, discovery_id, database, statement_id):
        with self.lock:
            self.row(discovery_id, database).update(statement=statement_id, claimed_at=self.now)

    def clear(self, kind, discovery_id, database):
        with self.lock:
            self.row(discovery_id, database).update(statement=None, claimed_at=None)

    def finish_check(self, discovery_id, database, ok, tables, why):
        with self.lock:
            self.row(discovery_id, database).update(open=ok, tables=tables, why=why, statement=None, claimed_at=None)

    def finish_read(self, discovery_id, database, rows, error, statement_id):
        with self.lock:
            row = self.row(discovery_id, database)
            if row["status"] != "running" or (row["statement"] or statement_id) != statement_id:
                return
            self.finished.append((discovery_id, database))
            row.update(status="ok" if rows is not None else "failed", found=rows, error=error, statement=None,
                       claimed_at=None, tables=len(rows) if rows is not None else None)

    def progress(self, kind, discovery_id):
        with self.lock:
            rows = [r for _, r in self._mine(discovery_id)]
            pending = sum(self._work(kind, r) for r in rows)
            if kind == wr.CHECK:
                return {"databases": len(rows), "pending": pending, "open": sum(r["open"] is True for r in rows),
                        "closed": sum(r["open"] is False for r in rows),
                        "tables": sum(r["tables"] or 0 for r in rows if r["open"])}
            return {"databases": len(rows), "pending": pending, "done": sum(r["status"] == "ok" for r in rows),
                    "failed": sum(r["status"] == "failed" for r in rows),
                    "tables": sum(r["tables"] or 0 for r in rows if r["status"] == "ok")}


class _Warehouse:
    """Each database's statement answers the states given for that database in turn, the last one repeating. Sent, it
    answers PENDING; with ``in_call`` it answers its first state then, as a statement that ends within its wait does.
    ``hold_s`` keeps each send that long (a statement's time on the source), and ``most`` is the most sends that were
    going at once, per server."""
    ENDED = warehouse.ENDED

    def __init__(self, answers, state="RUNNING", in_call=False, hold_s=0.0):
        self.answers, self.state, self.in_call, self.hold_s = answers, state, in_call, hold_s
        self.started_for, self.statements, self.waits, self.read_ids, self.cancelled = [], {}, [], [], []
        self.lock = threading.Lock()
        self.now_sending, self.most = {}, {}

    def start(self, statement, parameters, wait_s=None):
        values = {p["name"]: p["value"] for p in parameters}
        database, server = values["db"], values["conn"].lower()
        with self.lock:
            self.started_for.append(database)
            self.waits.append(wait_s)
            self.statements[f"s-{database}"] = (database, statement)
            self.now_sending[server] = self.now_sending.get(server, 0) + 1
            self.most[server] = max(self.most.get(server, 0), self.now_sending[server])
        try:
            if self.hold_s:
                time.sleep(self.hold_s)
            if self.in_call:
                answer = self._next(database)
                if isinstance(answer, Exception):
                    raise answer
                return {"statement_id": f"s-{database}", **answer}
            return {"statement_id": f"s-{database}", "state": "PENDING", "rows": None, "error": None}
        finally:
            with self.lock:
                self.now_sending[server] -= 1

    def _next(self, database):
        with self.lock:
            queue = self.answers[database]
            return queue.pop(0) if len(queue) > 1 else queue[0]

    def read(self, statement_id):
        with self.lock:
            self.read_ids.append(statement_id)
        answer = self._next(self.statements[statement_id][0] if statement_id in self.statements
                            else statement_id[len("s-"):])
        if isinstance(answer, Exception):
            raise answer
        return {"statement_id": statement_id, **answer}

    def cancel(self, statement_id):
        self.cancelled.append(statement_id)

    def warehouse_state(self):
        return self.state


def _ok(rows):
    return {"state": "SUCCEEDED", "rows": rows, "error": None}


def _failed(error):
    return {"state": "FAILED", "rows": None, "error": error}


SUMMARY_ROW = {"sch": "dbo", "tbl": "orders", "row_count": "12", "ct_enabled": "1", "select_cols": "id, amount",
               "excluded_cols": None, "pk_cols": "id", "pk_unreadable": "0", "pk_first_type": "int",
               "rowversion_col": None, "identity_col": None, "identity_type": None}


def test_a_check_opens_each_database_and_counts_its_tables_the_page_shown_first():
    store = _Store([f"db{i:02d}" for i in range(20)], wr.CHECK)
    fake = _Warehouse({f"db{i:02d}": [_ok([{"tables": str(i)}])] for i in range(20)})
    first = wr.advance(store, fake, wr.CHECK, "d1", "sql_a", first=["db19", "db18"])
    assert {"db18", "db19"} <= set(fake.started_for) and len(fake.started_for) == wr.IN_FLIGHT[wr.CHECK]
    assert "db17" not in fake.started_for, "the page shown is checked first"
    assert first["state"] == "RUNNING" and first["pending"] == 20 and first["warehouse"] == "RUNNING"
    _, statement = fake.statements["s-db18"]
    assert statement == "SELECT * FROM remote_query(:conn, database => :db, query => :q)"
    second = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert second["open"] == 16 and len(fake.started_for) == 20
    third = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert third["state"] == "SUCCEEDED" and third["open"] == 20 and third["closed"] == 0
    assert third["tables"] == sum(range(20)) and store.row("d1", "db07")["tables"] == 7


def test_checks_are_sent_together_with_a_short_wait_and_saved_by_the_step_that_sent_them():
    store = _Store([f"db{i:02d}" for i in range(20)], wr.CHECK)
    fake = _Warehouse({f"db{i:02d}": [_ok([{"tables": "2"}])] for i in range(20)}, in_call=True, hold_s=0.05)
    began = time.monotonic()
    step = wr.advance(store, fake, wr.CHECK, "d1", "sql_a", first=[f"db{i:02d}" for i in range(16)])
    assert time.monotonic() - began < 0.05 * 8, "sent at once, not one after another"
    assert fake.waits == [wr.CHECK_WAIT_S] * 16 and fake.read_ids == []
    assert step["open"] == 16 and step["pending"] == 4 and fake.most["sql_a"] >= 8


def test_a_database_the_login_cant_open_is_closed_with_why_in_plain_words_and_the_servers_message():
    store = _Store(["eng_001", "eng_150"], wr.CHECK)
    refused = ('[FAILED_JDBC.CONNECTION] Cannot open database "eng_150" requested by the login. The login failed.\n'
               "at com.microsoft...")
    fake = _Warehouse({"eng_001": [_ok([{"tables": "1000"}])], "eng_150": [_failed(refused)]})
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    done = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert done == {"databases": 2, "pending": 0, "open": 1, "closed": 1, "tables": 1000, "state": "SUCCEEDED",
                    "warehouse": "RUNNING"}
    why = store.row("d1", "eng_150")["why"]
    assert why.startswith("the connection's login has no access to this database: ask the server's admin for a user in it")
    assert 'Cannot open database "eng_150"' in why and "com.microsoft" not in why


def test_a_check_still_going_is_unknown_and_says_whether_the_warehouse_is_starting():
    store = _Store(["eng_001"], wr.CHECK)
    fake = _Warehouse({"eng_001": [{"state": "PENDING", "rows": None, "error": None}]}, state="STARTING")
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    step = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert step["state"] == "RUNNING" and step["pending"] == 1 and step["warehouse"] == "STARTING"
    assert store.row("d1", "eng_001")["open"] is None and fake.started_for == ["eng_001"]


def test_a_statement_that_cant_be_read_any_more_is_started_again():
    store = _Store(["eng_001"], wr.CHECK)
    fake = _Warehouse({"eng_001": [RuntimeError("statement not found"), _ok([{"tables": "3"}])]})
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert fake.started_for == ["eng_001", "eng_001"]
    assert wr.advance(store, fake, wr.CHECK, "d1", "sql_a")["open"] == 1


def test_a_read_saves_each_databases_tables_with_the_discovery_jobs_recommendations():
    store = _Store(["eng_001", "eng_002"], wr.READ, discovery_id="d9")
    fake = _Warehouse({"eng_001": [_ok([SUMMARY_ROW])], "eng_002": [_failed("Login failed for user 'x'.")]})
    wr.advance(store, fake, wr.READ, "d9", "sql_a")
    done = wr.advance(store, fake, wr.READ, "d9", "sql_a")
    assert done["state"] == "SUCCEEDED" and done["done"] == 1 and done["failed"] == 1 and done["tables"] == 1
    found = dict(zip(wr.TABLE_COLUMNS, store.row("d9", "eng_001")["found"][0]))
    assert found["discovery_id"] == "d9" and found["src_database"] == "eng_001" and found["src_table"] == "orders"
    assert found["load_type"] == "ct" and found["primary_key"] == "id" and found["select_list"] == "id, amount"
    assert store.row("d9", "eng_002")["error"] == "Login failed for user 'x'."
    _, statement = fake.statements["s-eng_001"]
    assert statement.startswith("SELECT * FROM remote_query(")


def test_a_pages_step_of_a_read_keeps_to_its_servers_cap():
    store = _Store([f"db{i:03d}" for i in range(100)], wr.READ, discovery_id="d9")
    fake = _Warehouse({f"db{i:03d}": [{"state": "RUNNING", "rows": None, "error": None}] for i in range(100)})
    caps = wr.ServerCaps(start=8)
    for _ in range(5):
        wr.advance(store, fake, wr.READ, "d9", "sql_a", caps=caps)
    assert len(fake.started_for) == 8 and len(store.in_flight(wr.READ, "d9")) == 8
    # Sent together, none waited for: one step starts them all, and the next collects the ones that ended.
    assert fake.waits == [0] * 8


def test_the_pages_steps_of_two_discoveries_of_one_server_share_its_cap_and_another_server_has_its_own():
    store = _Store()
    store.add("d1", [f"a{i:02d}" for i in range(20)], connection="sql_a")
    store.add("d2", [f"b{i:02d}" for i in range(20)], connection="SQL_A")
    store.add("d3", [f"c{i:02d}" for i in range(20)], connection="sql_b")
    fake = _Warehouse({n: [{"state": "RUNNING", "rows": None, "error": None}]
                       for p in "abc" for n in (f"{p}{i:02d}" for i in range(20))})
    caps = wr.ServerCaps(start=8)
    for _ in range(4):
        for did, connection in (("d1", "sql_a"), ("d2", "SQL_A"), ("d3", "sql_b")):
            wr.advance(store, fake, wr.READ, did, connection, caps=caps)
            assert store.going("sql_a") <= 8 and store.going("sql_b") <= 8
    assert store.going("sql_a") == 8 and store.going("sql_b") == 8
    assert len(store.in_flight(wr.READ, "d1")) + len(store.in_flight(wr.READ, "d2")) == 8


def test_checks_keep_16_going_for_the_page_shown_and_reads_start_at_a_cap_of_8():
    assert wr.IN_FLIGHT == {wr.CHECK: 16}
    assert (wr.START_CAP, wr.MIN_CAP, wr.MAX_CAP, wr.CAP_STEP) == (8, 4, 32, 4)


def test_a_statement_that_fails_to_start_frees_its_database_and_says_why():
    store = _Store(["eng_001"], wr.READ, discovery_id="d9")

    class Broken(_Warehouse):
        def start(self, statement, parameters, wait_s=None):
            raise RuntimeError("the warehouse was deleted\nmore")
    step = wr.advance(store, Broken({}), wr.READ, "d9", "sql_a")
    assert step["error"] == "the warehouse was deleted" and step["pending"] == 1
    assert store.row("d9", "eng_001")["claimed_at"] is None


# ---- the cap rule ----------------------------------------------------------------------------------------------------

def test_the_cap_holds_until_a_window_of_reads_is_timed_and_there_is_a_baseline():
    assert wr.next_cap(8, [1.0] * 7, 1.0, True) == 8
    assert wr.next_cap(8, [1.0] * 8, None, True) == 8


def test_the_cap_steps_up_by_4_while_reads_keep_their_time_and_the_server_is_held_at_it():
    assert wr.next_cap(8, [1.0] * 8, 1.0, True) == 12
    assert wr.next_cap(12, [1.2] * 8, 1.0, True) == 16
    assert wr.next_cap(8, [1.0] * 8, 1.0, False) == 8, "a cap the server isn't held at isn't raised"


def test_the_cap_holds_while_reads_slow_a_little():
    assert wr.next_cap(16, [1.4] * 8, 1.0, True) == 16
    assert wr.next_cap(16, [1.5] * 8, 1.0, True) == 16


def test_the_cap_steps_down_by_4_once_the_median_read_is_clearly_slower_than_at_the_start():
    assert wr.next_cap(16, [1.6] * 8, 1.0, True) == 12
    assert wr.next_cap(16, [1.0, 1.0, 1.0, 9.0, 9.0, 9.0, 9.0, 9.0], 1.0, False) == 12
    assert wr.next_cap(16, [9.0, 9.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0], 1.0, True) == 20, "the median, not the worst"


def test_the_cap_never_goes_below_4_or_above_32():
    assert wr.next_cap(4, [5.0] * 8, 1.0, True) == 4
    assert wr.next_cap(32, [1.0] * 8, 1.0, True) == 32
    assert wr.next_cap(64, [1.2] * 8, 1.0, False) == 32 and wr.next_cap(1, [1.2] * 8, 1.0, False) == 4


def test_a_servers_cap_starts_at_8_takes_its_baseline_there_and_moves_a_step_per_window():
    now = [0.0]
    caps = wr.ServerCaps(start=8, clock=lambda: now[0])
    assert caps.cap("sql_a") == 8
    caps.saturated("sql_a", True)
    for _ in range(7):
        assert caps.timed("sql_a", 1.0) == 8
    assert caps.timed("sql_a", 1.0) == 12                 # the starting level's median is 1 s, and 1 s is flat
    for _ in range(8):
        caps.timed("sql_a", 1.1)
    assert caps.cap("sql_a") == 16
    for _ in range(8):
        caps.timed("sql_a", 1.8)                          # past 1.5 times the starting level's: back a step
    assert caps.cap("sql_a") == 12
    assert caps.cap("sql_b") == 8, "each server has its own"
    now[0] += wr.CAP_FORGET_S + 1
    assert caps.cap("sql_a") == 8, "a server not read for a while starts again"


@pytest.mark.parametrize("error,plain", [
    ("Login failed for user 'lfc_discover'.", "the connection's login has no access to this database"),
    ("Database 'eng_9' is not currently available.", "the database isn't available on the server now"),
    ("Something else broke", "the database couldn't be opened through the connection"),
    (None, "the database couldn't be opened through the connection"),
])
def test_why_closed_says_it_plainly_first(error, plain):
    assert wr.why_closed(error).startswith(plain)
