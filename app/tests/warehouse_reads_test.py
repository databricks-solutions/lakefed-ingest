"""Discovery's per-database checks and reads on the SQL warehouse (warehouse_reads.advance), with the Lakebase store and
the warehouse faked: what each step starts, saves and answers."""
import pytest

import warehouse
import warehouse_reads as wr


class _Store:
    """The discovery rows of one discovery in memory, with the store's calls."""

    def __init__(self, names, kind):
        status = "listed" if kind == wr.CHECK else "running"
        self.rows = {n: {"status": status, "statement": None, "claimed": False, "open": None, "tables": None,
                         "error": None, "why": None, "found": None} for n in names}

    def _work(self, kind, row):
        return row["open"] is None and row["status"] == "listed" if kind == wr.CHECK else row["status"] == "running"

    def in_flight(self, kind, discovery_id):
        return [(n, r["statement"]) for n, r in sorted(self.rows.items()) if self._work(kind, r) and r["statement"]]

    def claim(self, kind, discovery_id, first, limit, claim_s):
        free = [n for n, r in self.rows.items() if self._work(kind, r) and not r["statement"] and not r["claimed"]]
        free.sort(key=lambda n: (n not in first, n))
        for n in free[:limit]:
            self.rows[n]["claimed"] = True
        return free[:limit]

    def started(self, kind, discovery_id, database, statement_id):
        self.rows[database]["statement"] = statement_id

    def clear(self, kind, discovery_id, database):
        self.rows[database].update(statement=None, claimed=False)

    def finish_check(self, discovery_id, database, ok, tables, why):
        self.rows[database].update(open=ok, tables=tables, why=why, statement=None, claimed=False)

    def finish_read(self, discovery_id, database, rows, error):
        row = self.rows[database]
        row.update(status="ok" if rows is not None else "failed", found=rows, error=error, statement=None, claimed=False,
                   tables=len(rows) if rows is not None else None)

    def progress(self, kind, discovery_id):
        rows = list(self.rows.values())
        pending = sum(self._work(kind, r) for r in rows)
        if kind == wr.CHECK:
            return {"databases": len(rows), "pending": pending, "open": sum(r["open"] is True for r in rows),
                    "closed": sum(r["open"] is False for r in rows),
                    "tables": sum(r["tables"] or 0 for r in rows if r["open"])}
        return {"databases": len(rows), "pending": pending, "done": sum(r["status"] == "ok" for r in rows),
                "failed": sum(r["status"] == "failed" for r in rows),
                "tables": sum(r["tables"] or 0 for r in rows if r["status"] == "ok")}


class _Warehouse:
    """Each database's statement answers the states given for that database in turn, the last one repeating."""
    ENDED = warehouse.ENDED

    def __init__(self, answers, state="RUNNING"):
        self.answers, self.state, self.started_for, self.statements, self.waits = answers, state, [], {}, []

    def start(self, statement, parameters, wait_s=None):
        database = {p["name"]: p["value"] for p in parameters}["db"]
        self.started_for.append(database)
        self.waits.append(wait_s)
        self.statements[f"s-{database}"] = (database, statement)
        return {"statement_id": f"s-{database}", "state": "PENDING", "rows": None, "error": None}

    def read(self, statement_id):
        database = self.statements[statement_id][0]
        queue = self.answers[database]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return {"statement_id": statement_id, **answer}

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
    assert fake.started_for[:2] == ["db18", "db19"] and len(fake.started_for) == wr.IN_FLIGHT[wr.CHECK]
    assert first["state"] == "RUNNING" and first["pending"] == 20 and first["warehouse"] == "RUNNING"
    _, statement = fake.statements["s-db18"]
    assert statement == "SELECT * FROM remote_query(:conn, database => :db, query => :q)"
    second = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert second["open"] == 16 and len(fake.started_for) == 20
    third = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert third["state"] == "SUCCEEDED" and third["open"] == 20 and third["closed"] == 0
    assert third["tables"] == sum(range(20)) and store.rows["db07"]["tables"] == 7


def test_a_database_the_login_cant_open_is_closed_with_why_in_plain_words_and_the_servers_message():
    store = _Store(["eng_001", "eng_150"], wr.CHECK)
    refused = ('[FAILED_JDBC.CONNECTION] Cannot open database "eng_150" requested by the login. The login failed.\n'
               "at com.microsoft...")
    fake = _Warehouse({"eng_001": [_ok([{"tables": "1000"}])], "eng_150": [_failed(refused)]})
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    done = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert done == {"databases": 2, "pending": 0, "open": 1, "closed": 1, "tables": 1000, "state": "SUCCEEDED",
                    "warehouse": "RUNNING"}
    why = store.rows["eng_150"]["why"]
    assert why.startswith("the connection's login has no access to this database: ask the server's admin for a user in it")
    assert 'Cannot open database "eng_150"' in why and "com.microsoft" not in why


def test_a_check_still_going_is_unknown_and_says_whether_the_warehouse_is_starting():
    store = _Store(["eng_001"], wr.CHECK)
    fake = _Warehouse({"eng_001": [{"state": "PENDING", "rows": None, "error": None}]}, state="STARTING")
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    step = wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert step["state"] == "RUNNING" and step["pending"] == 1 and step["warehouse"] == "STARTING"
    assert store.rows["eng_001"]["open"] is None and fake.started_for == ["eng_001"]


def test_a_statement_that_cant_be_read_any_more_is_started_again():
    store = _Store(["eng_001"], wr.CHECK)
    fake = _Warehouse({"eng_001": [RuntimeError("statement not found"), _ok([{"tables": "3"}])]})
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    wr.advance(store, fake, wr.CHECK, "d1", "sql_a")
    assert fake.started_for == ["eng_001", "eng_001"]
    assert wr.advance(store, fake, wr.CHECK, "d1", "sql_a")["open"] == 1


def test_a_read_saves_each_databases_tables_with_the_discovery_jobs_recommendations():
    store = _Store(["eng_001", "eng_002"], wr.READ)
    fake = _Warehouse({"eng_001": [_ok([SUMMARY_ROW])], "eng_002": [_failed("Login failed for user 'x'.")]})
    wr.advance(store, fake, wr.READ, "d9", "sql_a")
    done = wr.advance(store, fake, wr.READ, "d9", "sql_a")
    assert done["state"] == "SUCCEEDED" and done["done"] == 1 and done["failed"] == 1 and done["tables"] == 1
    found = dict(zip(wr.TABLE_COLUMNS, store.rows["eng_001"]["found"][0]))
    assert found["discovery_id"] == "d9" and found["src_database"] == "eng_001" and found["src_table"] == "orders"
    assert found["load_type"] == "ct" and found["primary_key"] == "id" and found["select_list"] == "id, amount"
    assert store.rows["eng_002"]["error"] == "Login failed for user 'x'."
    _, statement = fake.statements["s-eng_001"]
    assert statement.startswith("SELECT * FROM remote_query(")


def test_a_read_never_keeps_more_than_its_share_of_statements_going():
    store = _Store([f"db{i:03d}" for i in range(100)], wr.READ)
    fake = _Warehouse({f"db{i:03d}": [{"state": "RUNNING", "rows": None, "error": None}] for i in range(100)})
    for _ in range(5):
        wr.advance(store, fake, wr.READ, "d9", "sql_a")
    cap = wr.IN_FLIGHT[wr.READ]
    assert len(fake.started_for) == cap and len(store.in_flight(wr.READ, "d9")) == cap
    # Sent together, none waited for: one step starts them all, and the next collects the ones that ended.
    assert fake.waits == [0] * wr.IN_FLIGHT[wr.READ]


def test_a_background_read_keeps_to_8_and_leaves_room_for_the_check_of_the_page_shown():
    assert wr.IN_FLIGHT == {wr.CHECK: 16, wr.READ: 8}


def test_a_statement_that_fails_to_start_frees_its_database_and_says_why():
    store = _Store(["eng_001"], wr.READ)

    class Broken(_Warehouse):
        def start(self, statement, parameters, wait_s=None):
            raise RuntimeError("the warehouse was deleted\nmore")
    step = wr.advance(store, Broken({}), wr.READ, "d9", "sql_a")
    assert step["error"] == "the warehouse was deleted" and step["pending"] == 1
    assert store.rows["eng_001"]["claimed"] is False


@pytest.mark.parametrize("error,plain", [
    ("Login failed for user 'lfc_discover'.", "the connection's login has no access to this database"),
    ("Database 'eng_9' is not currently available.", "the database isn't available on the server now"),
    ("Something else broke", "the database couldn't be opened through the connection"),
    (None, "the database couldn't be opened through the connection"),
])
def test_why_closed_says_it_plainly_first(error, plain):
    assert wr.why_closed(error).startswith(plain)
