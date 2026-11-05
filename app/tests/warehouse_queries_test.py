"""The Lakebase statements of discovery's warehouse checks and reads (queries.warehouse_*): their placeholders match
the parameters main.WarehouseReadStore passes, and each keeps to the rows of its kind."""
import queries as q
import warehouse_reads as wr


def _marks(sql):
    return sql.count("%s")


def test_each_statement_takes_the_parameters_the_store_passes():
    for kind in (wr.CHECK, wr.READ):
        assert _marks(q.warehouse_in_flight_sql(kind)) == 1
        assert _marks(q.warehouse_claim_sql(kind)) == 4
        assert _marks(q.warehouse_progress_sql(kind)) == 1
    assert _marks(q.warehouse_started_sql()) == 3 and _marks(q.warehouse_clear_sql()) == 2
    assert _marks(q.warehouse_check_done_sql()) == 5 and _marks(q.warehouse_read_done_sql()) == 8
    assert _marks(q.warehouse_read_rows_sql()) == 5 and _marks(q.warehouse_left_out_sql()) == 2
    assert _marks(q.warehouse_named_rows_sql(3)) == 15
    assert _marks(q.insert_discovered_tables_sql(wr.TABLE_COLUMNS, 2)) == 2 * len(wr.TABLE_COLUMNS)
    assert _marks(q.warehouse_reads_lock_sql()) == 1 and _marks(q.warehouse_going_sql()) == 2
    assert _marks(q.warehouse_server_load_sql()) == 3 and _marks(q.warehouse_want_checks_sql()) == 4
    assert _marks(q.warehouse_free_checks_sql()) == 3
    assert _marks(q.warehouse_claim_reads_sql()) == 5 and _marks(q.warehouse_collect_sql()) == 3
    assert _marks(q.warehouse_hold_sql()) == 2 and _marks(q.warehouse_read_work_sql()) == 3
    assert _marks(q.discovery_connection_sql()) == 1 and _marks(q.draft_drop_sql()) == 3


def test_a_servers_places_are_counted_in_one_query_with_two_filtered_counts():
    load = q.warehouse_server_load_sql()
    assert load.count(f"from {q.SCHEMA}.discovery") == 1, "one scan of the discovery table, not two"
    assert load.count("count(*) filter (where") == 2 and "as going" in load and "as waiting" in load
    assert "lower(src_connection) = p.server" in load and q.WAREHOUSE_WORK["read"] in load
    assert q.WAREHOUSE_WORK["check"] in load and "wanted_at" in load and "'via' = 'warehouse'" in load


def test_a_servers_reads_are_counted_and_claimed_under_a_transaction_lock_of_its_own():
    assert "pg_advisory_xact_lock" in q.warehouse_reads_lock_sql() and "advisory_lock(" not in q.warehouse_reads_lock_sql()
    assert q.warehouse_reads_lock_key("sql_a") == "lakefed_ingest/warehouse_reads/sql_a"
    assert not q.warehouse_reads_lock_key("sql_a").startswith(q.collection_lock_key(""))
    going, claim = q.warehouse_going_sql(), q.warehouse_claim_reads_sql()
    for sql in (going, claim):
        assert "lower(src_connection) = %s" in sql and "listing ->> 'via' = 'warehouse'" in sql
        assert q.WAREHOUSE_WORK["read"] in sql
    assert q.WAREHOUSE_WORK["check"] in going, "a check in flight is one of the server's places"
    assert "(listing ->> 'claimed_at')::timestamptz >= now()" in going
    assert "for update skip locked" in claim and "order by array_position(%s::text[], discovery_id), src_database" in claim
    assert "for update skip locked" in q.warehouse_collect_sql() and "listing ? 'statement_id'" in q.warehouse_collect_sql()


def test_a_dropped_database_is_no_read_and_counts_in_no_progress_wait_or_failure():
    assert "? 'dropped'" in q.WAREHOUSE_WORK["read"] and "dropped" not in q.WAREHOUSE_WORK["check"]
    assert f"not ({q.DROPPED})" in q.warehouse_progress_sql(wr.READ)
    assert f"not ({q.DROPPED})" in q.draft_reads_sql() and f"not ({q.DROPPED})" in q.draft_failed_sql()
    for sql in (q.warehouse_read_work_sql(), q.warehouse_claim_reads_sql(), q.warehouse_hold_sql(),
                q.warehouse_collect_sql(), q.warehouse_in_flight_sql(wr.READ)):
        assert q.WAREHOUSE_WORK["read"] in sql
    drop = q.draft_drop_sql()
    assert "jsonb_build_object('dropped', now()::text)" in drop and "- 'statement_id' - 'claimed_at'" in drop
    assert "not coalesce((listing ->> 'whole_server')::boolean, false)" in drop
    assert "returning old.src_database, old.statement_id" in drop
    assert "do update set listing = lakefed_ingest.discovery.listing - 'dropped' - 'claimed_at' " \
           "where lakefed_ingest.discovery.listing ? 'dropped'" in q.warehouse_named_rows_sql(1)
    assert "listing ? 'draft' and not listing ? 'discarded'" in q.draft_claim_sql() and "dropped" not in q.draft_claim_sql()


def test_a_check_works_on_listed_databases_not_checked_yet_and_a_read_on_running_ones():
    assert "status = 'listed' and (listing -> 'open') is null" in q.warehouse_claim_sql(wr.CHECK)
    assert "status = 'running'" in q.warehouse_claim_sql(wr.READ)
    assert "for update skip locked" in q.warehouse_claim_sql(wr.READ)


def test_a_check_takes_only_the_page_shown_and_a_read_takes_it_first():
    check, read = q.warehouse_claim_sql(wr.CHECK), q.warehouse_claim_sql(wr.READ)
    assert "and src_database = any(%s::text[]) order by src_database limit %s" in check
    assert "order by (src_database = any(%s::text[])) desc, src_database limit %s" in read
    assert _marks(q.warehouse_unchecked_sql()) == 2 and "src_database = any(%s::text[])" in q.warehouse_unchecked_sql()


def test_a_second_answer_for_a_database_changes_nothing():
    done = q.warehouse_read_done_sql()
    assert "and status = 'running' and coalesce(listing ->> 'statement_id', %s::text) = %s::text " in done
    assert done.endswith("returning src_database")
    assert q.warehouse_check_done_sql().endswith("and status = 'listed'")
    assert "on conflict (discovery_id, src_database, src_schema, src_table) do nothing" in \
        q.insert_discovered_tables_sql(wr.TABLE_COLUMNS, 1)


def test_the_listing_page_shows_each_databases_check_and_the_totals_count_them():
    assert "(listing ->> 'open')::boolean as open" in q.discovery_listing_sql()
    assert "listing ->> 'why' as why" in q.discovery_listing_sql()
    assert "as closed" in q.discovery_totals_sql() and "as tables" in q.discovery_totals_sql()


def test_a_read_saves_its_tables_without_a_delete(monkeypatch):
    """The app has no DELETE on Lakebase (grant_app_access gives select, insert and update), so a statement with one
    fails there for every database, even when no row matches; the tables' owner, as the Postgres tests run, never
    sees it."""
    import contextlib
    import pytest
    pytest.importorskip("fastapi")
    import main
    sent = []

    class Cursor:
        description = [type("Column", (), {"name": "src_database"})()]

        def execute(self, sql, params=()):
            sent.append(sql)

        def fetchall(self):
            return [("eng_001",)]

    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor())))
    row = tuple(None for _ in wr.TABLE_COLUMNS)
    main.WarehouseReadStore().finish_read("d1", "eng_001", [row, row], None, "s-1")
    assert sent[0] == q.warehouse_read_done_sql() and len(sent) == 2
    assert not any("delete" in sql.lower() for sql in sent)


def test_a_read_its_database_no_longer_waits_on_saves_no_tables(monkeypatch):
    import contextlib
    import pytest
    pytest.importorskip("fastapi")
    import main
    sent = []

    class Cursor:
        description = [type("Column", (), {"name": "src_database"})()]

        def execute(self, sql, params=()):
            sent.append((sql, params))

        def fetchall(self):
            return []

    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor())))
    main.WarehouseReadStore().finish_read("d1", "eng_001", [tuple(None for _ in wr.TABLE_COLUMNS)], None, "s-old")
    assert sent == [(q.warehouse_read_done_sql(), ("ok", 1, None, "d1", "eng_001", "s-old", "s-old", "ok"))]


def test_no_statement_of_the_app_deletes_rows():
    """Every string in the app's code that reads as SQL: none deletes (the app's role can't)."""
    import io
    import re
    import tokenize
    from pathlib import Path
    found = []
    for path in sorted((Path(__file__).resolve().parents[1] / "src").glob("*.py")):
        for token in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            # From Python 3.12 an f-string's text is its own token (FSTRING_MIDDLE), not a STRING.
            text = token.type in (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING))
            if text and re.search(r"\bdelete\s+from\b", token.string, re.I) \
                    and not token.string.lstrip("rbfuRBFU").startswith(('"""', "'''")):
                found.append(f"{path.name}:{token.start[0]}")
    assert found == [], f"a DELETE statement in the app, which has no DELETE privilege: {found}"


def _filled(sql, params):
    """The statement with each placeholder replaced by its parameter, in order, to read which goes where."""
    parts = sql.split("%s")
    assert len(parts) == len(params) + 1
    return "".join(part + (repr(params[i]) if i < len(params) else "") for i, part in enumerate(parts))


def test_the_stores_read_claim_passes_each_parameter_to_its_place(monkeypatch):
    import contextlib
    import pytest
    pytest.importorskip("fastapi")
    import main
    sent = []

    class Cursor:
        description = None

        def execute(self, sql, params=()):
            sent.append(_filled(sql, params))
            if "as going" in sql:
                self.description, self.rows = self._named("going", "waiting"), [(3, 0)]
            else:
                self.description, self.rows = self._named("discovery_id", "src_database"), [("d2", "eng_001")]

        def _named(self, *names):
            return [type("Column", (), {"name": n})() for n in names]

        def fetchall(self):
            return self.rows

    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor())))
    claimed = main.WarehouseReadStore().claim_reads("sql_a", ["d2", "d1"], 8, 4, 60)
    assert claimed == [("d2", "eng_001")]
    lock, load, claim = sent
    assert lock == "select pg_advisory_xact_lock(hashtextextended('lakefed_ingest/warehouse_reads/sql_a', 0))"
    assert "'sql_a'::text as server" in load and "60::float as claim_s" in load and "10::float as want_s" in load
    assert "lower(src_connection) = 'sql_a' and discovery_id = any(['d2', 'd1']::text[])" in claim
    assert "make_interval(secs => 60)" in claim and "array_position(['d2', 'd1']::text[], discovery_id)" in claim
    assert claim.endswith("limit 4 for update skip locked) c where d.discovery_id = c.discovery_id and "
                          "d.src_database = c.src_database returning d.discovery_id, d.src_database")


def test_the_stores_other_read_calls_pass_each_parameter_to_its_place(monkeypatch):
    import pytest
    pytest.importorskip("fastapi")
    import main
    sent = []
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): sent.append(_filled(sql, params)) or [])
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): sent.append(_filled(sql, params)))
    store = main.WarehouseReadStore()
    store.collect("d1", 60, 5)
    store.hold([("d1", "eng_001"), ("d2", "eng_009")])
    store.read_work(60, 21600, 7200)
    assert store.started("read", "d1", "eng_001", "s-1") is False
    store.finish_read("d1", "eng_002", None, "Login failed", "s-2")
    collect, hold, work, started, failed = sent
    assert "discovery_id = 'd1'" in collect and "make_interval(secs => 60)" in collect and "limit 5 for update" in collect
    assert "unnest(['d1', 'd2']::text[], ['eng_001', 'eng_009']::text[])" in hold
    assert work.startswith("with p as (select 60::float as claim_s, 21600::float as max_age, 7200::float as draft_ttl)")
    assert "jsonb_build_object('statement_id', 's-1'::text" in started and "src_database = 'eng_001'" in started
    assert "set status = 'failed', tables = None, error = 'Login failed'" in failed
    assert "coalesce(listing ->> 'statement_id', 's-2'::text) = 's-2'::text and ('failed'::text = 'ok'" in failed


def test_a_check_claim_that_finds_the_server_full_wants_at_most_half_the_cap_in_the_order_it_claims(monkeypatch):
    import contextlib
    import pytest
    pytest.importorskip("fastapi")
    import main
    sent = []

    class Cursor:
        description = None

        def execute(self, sql, params=()):
            sent.append(_filled(sql, params))
            self.description = [type("Column", (), {"name": n})() for n in
                                (("going",) if "count(*) as going" in sql else ("src_database",))]
            self.rows = [(8,)] if "count(*) as going" in sql else []

        def fetchall(self):
            return self.rows

    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor())))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"free": True}])
    names = [f"c{i:03d}" for i in range(100)]
    assert main.WarehouseReadStore().claim_checks("sql_a", "d2", names, 16, 8, 60) == []
    lock, going, wanted = sent
    assert lock.startswith("select pg_advisory_xact_lock(")
    assert "discovery_id = 'd2'" in wanted and "make_interval(secs => 60)" in wanted
    assert "wanted_at" in wanted and wanted.endswith("order by src_database limit 4)")
    sent.clear()
    main.WarehouseReadStore().claim_checks("sql_a", "d2", names, 2, 8, 60)
    assert sent[-1].endswith("order by src_database limit 2)"), "only 2 of the discovery's check slots are free"


def test_a_check_poll_with_nothing_free_to_claim_takes_no_lock_and_one_with_something_takes_it(monkeypatch):
    import contextlib
    import pytest
    pytest.importorskip("fastapi")
    import main
    looked, locked = [], []

    class Cursor:
        def execute(self, sql, params=()):
            locked.append(sql)
            counted = "count(*) as going" in sql
            self.description = [type("Column", (), {"name": "going" if counted else "src_database"})()]
            self.rows = [(0,)] if counted else []

        def fetchall(self):
            return self.rows

    free = [False]
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): looked.append(_filled(sql, params)) or [{"free": free[0]}])
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor())))
    store = main.WarehouseReadStore()
    assert store.claim_checks("sql_a", "d2", ["c001", "c002"], 16, 8, 60) == []
    assert locked == [] and len(looked) == 1, "every name is claimed or checked: one quick look, no lock"
    assert "discovery_id = 'd2'" in looked[0] and "any(['c001', 'c002']::text[])" in looked[0]
    free[0] = True
    store.claim_checks("sql_a", "d2", ["c001", "c002"], 16, 8, 60)
    assert locked and locked[0].startswith("select pg_advisory_xact_lock("), "a free name is claimed under the lock"
