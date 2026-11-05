"""The reads through the warehouse against a real Postgres with the Lakebase schema, through main.WarehouseReadStore
and the Lakebase pool itself: each server's cap counted across discoveries and concurrent claimers under its
transaction lock, the background reader and a page's step at once (SKIP LOCKED), dropped databases left out of the
claim, the progress and Create's wait (and the drafts' purge), and a restart reading saved statements again. The
warehouse is faked (warehouse_reads_test._Warehouse).

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import background_reads as br  # noqa: E402
import main  # noqa: E402
import queries as q  # noqa: E402
import warehouse_reads as wr  # noqa: E402
from warehouse_reads_test import SUMMARY_ROW, _Warehouse, _failed, _ok  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
RUNNING = {"state": "RUNNING", "rows": None, "error": None}


@pytest.fixture
def owner(monkeypatch):
    """A database of the test's own with the Lakebase schema, the app's Lakebase calls pointed at it (through the
    pool), and an owner connection for the setup and the asserts."""
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_reads_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
    main.db.close_idle()
    conn = psycopg.connect(target, autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        yield conn
    finally:
        main.db.close_idle()
        conn.close()
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _read(conn, names, connection="sql_a", marks=None):
    """A new read of these databases through the warehouse, as _start_warehouse_read writes it: its discovery id."""
    did = uuid.uuid4().hex[:12]
    conn.execute(q.warehouse_named_rows_sql(len(names)),
                 tuple(v for n in names for v in (did, connection, n, "op@example.com", json.dumps(marks or {}))))
    return did


def _draft(conn, names):
    return _read(conn, names, marks={"draft": True, "draft_by": "op@example.com",
                                     "touched_at": datetime.now(timezone.utc).isoformat()})


def _listed(conn, names):
    """A listing of these databases through the warehouse, as the connection test saves it: its discovery id."""
    did = uuid.uuid4().hex[:12]
    listing = json.dumps({"via": "warehouse"})
    conn.execute(q.insert_discovery_rows_sql(len(names)),
                 tuple(v for n in names
                       for v in (did, "sqlserver", "sql_a", n, "listed", None, None, "op@example.com", listing)))
    return did


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    columns = [d.name for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _row(conn, did, database):
    return _rows(conn, "select * from lakefed_ingest.discovery where discovery_id = %s and src_database = %s",
                 (did, database))[0]


def _going(conn, server="sql_a"):
    return _rows(conn, q.warehouse_going_sql(), (server, wr.CLAIM_S))[0]["going"]


def _lapse(conn, did, database):
    """The claim on a database as an app instance that stopped leaves it: older than CLAIM_S."""
    conn.execute("update lakefed_ingest.discovery set listing = listing || jsonb_build_object('claimed_at', "
                 "(now() - interval '5 minutes')::text) where discovery_id = %s and src_database = %s", (did, database))


def test_concurrent_claimers_of_two_discoveries_of_one_server_never_pass_its_cap(owner):
    store = main.WarehouseReadStore()
    first = _read(owner, [f"a{i:02d}" for i in range(30)])
    second = _read(owner, [f"b{i:02d}" for i in range(30)], connection="SQL_A")
    other = _read(owner, [f"c{i:02d}" for i in range(30)], connection="sql_b")
    claimed, failures = [], []

    def claimer(ids):
        try:
            for _ in range(10):
                got = store.claim_reads("sql_a", ids, 8, 3, wr.CLAIM_S)
                claimed.extend(got)
        except Exception as e:
            failures.append(e)
    threads = [threading.Thread(target=claimer, args=(ids,)) for ids in
               ([first, second], [second, first], [first], [second])]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert len(claimed) == 8 and len(set(claimed)) == 8 and _going(owner) == 8
    assert store.claim_reads("sql_a", [first, second], 8, 8, wr.CLAIM_S) == []
    got = store.claim_reads("sql_b", [other], 8, 8, wr.CLAIM_S)
    assert len(got) == 8, "another server has its own cap"
    held = owner.execute("select count(*) from pg_locks where locktype = 'advisory' and database = "
                         "(select oid from pg_database where datname = current_database())").fetchone()[0]
    assert held == 0, "a transaction lock, gone with its transaction"


def test_checks_and_reads_of_two_discoveries_of_one_server_share_one_cap(owner):
    store = main.WarehouseReadStore()
    checks = [f"c{i:02d}" for i in range(12)]
    checking = _listed(owner, checks)
    reading = _read(owner, [f"a{i:02d}" for i in range(10)], connection="SQL_A")
    claimed = store.claim_checks("sql_a", checking, checks, 16, 8, wr.CLAIM_S)
    assert len(claimed) == 8 and _going(owner) == 8
    assert store.claim_reads("sql_a", [reading], 8, 8, wr.CLAIM_S) == [], "the 4 checks left wait for a place"
    for database in claimed[:5]:
        store.finish_check(checking, database, True, 1, None)
    assert _going(owner) == 3
    got = store.claim_reads("sql_a", [reading], 8, 8, wr.CLAIM_S)
    assert len(got) == 1 and _going(owner) == 4, "one read in the room the 4 waiting checks leave"


def test_a_page_of_unchecked_databases_holds_places_for_half_the_cap_and_reads_keep_the_rest(owner):
    store = main.WarehouseReadStore()
    page = [f"c{i:03d}" for i in range(100)]
    checking = _listed(owner, page)
    reading = _read(owner, [f"a{i:02d}" for i in range(20)], connection="SQL_A")
    started = store.claim_reads("sql_a", [reading], 8, 8, wr.CLAIM_S)
    assert len(started) == 8 and _going(owner) == 8
    assert store.claim_checks("sql_a", checking, page, 16, 8, wr.CLAIM_S) == [], "the server is full"
    wanted = _rows(owner, "select count(*) as n from lakefed_ingest.discovery where discovery_id = %s "
                          "and listing ? 'wanted_at'", (checking,))[0]["n"]
    assert wanted == 4, "half the cap, not the 100 names of the page"
    for _, database in started:
        store.started(wr.READ, reading, database, f"s-{database}")
        store.finish_read(reading, database, [], None, f"s-{database}")
    got = store.claim_reads("sql_a", [reading], 8, 8, wr.CLAIM_S)
    assert len(got) == 4, "reads keep the other half of the server's places while the page waits"


def test_a_claim_waits_for_the_servers_lock_held_elsewhere(owner):
    import psycopg
    store = main.WarehouseReadStore()
    did = _read(owner, ["eng_001", "eng_002"])
    other = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=owner.info.dbname))
    try:
        other.execute(q.warehouse_reads_lock_sql(), (q.warehouse_reads_lock_key("sql_a"),))
        got = []
        thread = threading.Thread(target=lambda: got.append(store.claim_reads("sql_a", [did], 8, 8, wr.CLAIM_S)))
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive() and got == [], "the claim waits for the lock"
        other.commit()
        thread.join(5)
        assert len(got[0]) == 2
    finally:
        other.close()


def test_the_background_reader_and_a_pages_step_at_once_never_start_one_database_twice(owner):
    store = main.WarehouseReadStore()
    names = [f"eng_{i:03d}" for i in range(40)]
    did = _read(owner, names)
    fake = _Warehouse({n: [RUNNING, RUNNING, _ok([SUMMARY_ROW])] for n in names})
    reader = br.Reader(store, fake, cap=8, sleep=lambda s: time.sleep(0.01))
    reader.start_workers()
    deadline = time.monotonic() + 60
    while store.progress(wr.READ, did)["pending"]:
        assert time.monotonic() < deadline, "the reads didn't end"
        reader.step()
        wr.advance(store, fake, wr.READ, did, "sql_a", cap=8)
        assert _going(owner) <= 8
        time.sleep(0.01)
    assert sorted(fake.started_for) == names, "each database sent once"
    assert _rows(owner, q.warehouse_progress_sql(wr.READ), (did,)) == \
        [{"databases": 40, "pending": 0, "done": 40, "failed": 0, "tables": 40}]


def test_a_dropped_database_is_left_out_of_the_claim_the_progress_and_creates_wait_and_taken_back_when_ticked(owner):
    store = main.WarehouseReadStore()
    did = _draft(owner, ["eng_001", "eng_002", "eng_003", "eng_004"])
    store.started(wr.READ, did, "eng_002", "s-2")
    store.finish_read(did, "eng_003", [], None, "s-3")                       # read already: kept when dropped
    store.finish_read(did, "eng_004", None, "Login failed", "s-4")            # failed: no longer blocks Create
    dropped = _rows(owner, q.draft_drop_sql(), (did, ["eng_002", "eng_003", "eng_004"], did))
    assert sorted((r["src_database"], r["statement_id"]) for r in dropped) == \
        [("eng_002", "s-2"), ("eng_003", None), ("eng_004", None)]
    assert "statement_id" not in _row(owner, did, "eng_002")["listing"]
    assert _rows(owner, q.draft_drop_sql(), (did, ["eng_002"], did)) == [], "dropped once"
    got = store.claim_reads("sql_a", [did], 8, 8, wr.CLAIM_S)
    assert got == [(did, "eng_001")]
    assert _rows(owner, q.warehouse_progress_sql(wr.READ), (did,)) == \
        [{"databases": 1, "pending": 1, "done": 0, "failed": 0, "tables": 0}]
    reads = _rows(owner, q.draft_reads_sql(), ([did],))[0]
    assert (reads["databases"], reads["pending"], reads["done"], reads["failed"]) == (1, 1, 0, 0)
    assert _rows(owner, q.draft_failed_sql(), ([did],)) == []
    # The cancelled statement's answer neither fails the dropped database nor ends it.
    store.finish_read(did, "eng_002", None, "the read ended CANCELED", "s-2")
    assert _row(owner, did, "eng_002")["status"] == "running"
    work = _rows(owner, q.warehouse_read_work_sql(), (wr.CLAIM_S, br.MAX_AGE_S, 7200))
    assert [(w["discovery_id"], w["waiting"]) for w in work] == [], "eng_001 is claimed and the rest dropped"
    # Ticked again: the draft grows by them again, and only the one never read is read.
    owner.execute(q.warehouse_named_rows_sql(2), (did, "sql_a", "eng_002", "op", "{}", did, "sql_a", "eng_003", "op", "{}"))
    assert "dropped" not in _row(owner, did, "eng_002")["listing"] and _row(owner, did, "eng_003")["status"] == "ok"
    got = store.claim_reads("sql_a", [did], 8, 8, wr.CLAIM_S)
    assert got == [(did, "eng_002")]


def test_a_draft_with_dropped_databases_is_purged_whole_and_a_job_takes_them_with_the_rest(owner):
    import delete_job
    store = main.WarehouseReadStore()
    left = _draft(owner, ["eng_001", "eng_002"])
    taken = _draft(owner, ["eng_001", "eng_002"])
    for did in (left, taken):
        found = wr.table_rows(did, "eng_002", [SUMMARY_ROW])
        store.finish_read(did, "eng_002", found, None, "s-2")
        owner.execute(q.draft_drop_sql(), (did, ["eng_002"], did))
    owner.execute(q.draft_claim_sql(), (7, [taken]))
    owner.execute(q.draft_discard_sql(), (left,))
    with owner.cursor() as cur, cur.connection.transaction():
        purged = delete_job.purge_drafts(cur, 7200)
    assert purged == {"drafts": 1, "databases": 2, "tables": 1}
    kept = _rows(owner, "select src_database, listing from lakefed_ingest.discovery where discovery_id = %s "
                        "order by src_database", (taken,))
    assert [r["listing"].get("job_id") for r in kept] == [7, 7] and "draft" not in kept[1]["listing"]
    assert _rows(owner, "select count(*) as n from lakefed_ingest.discovered_table where discovery_id = %s",
                 (taken,)) == [{"n": 1}]


def test_after_a_restart_the_reader_reads_a_saved_statement_again_and_sends_a_lapsed_claim_once(owner):
    store = main.WarehouseReadStore()
    did = _read(owner, ["eng_001", "eng_002", "eng_003"])
    store.claim_reads("sql_a", [did], 8, 2, wr.CLAIM_S)                      # eng_001 and eng_002, before the restart
    store.started(wr.READ, did, "eng_001", "s-eng_001")
    _lapse(owner, did, "eng_001")
    _lapse(owner, did, "eng_002")
    work = _rows(owner, q.warehouse_read_work_sql(), (wr.CLAIM_S, br.MAX_AGE_S, 7200))
    assert [(w["discovery_id"], w["waiting"], w["orphaned"]) for w in work] == [(did, 2, 1)]
    fake = _Warehouse({n: [_ok([SUMMARY_ROW])] for n in ("eng_001", "eng_002", "eng_003")}, in_call=True)
    reader = br.Reader(store, fake, cap=8, sleep=lambda s: time.sleep(0.01))
    reader.start_workers()
    deadline = time.monotonic() + 30
    while store.progress(wr.READ, did)["pending"] or reader.held():
        assert time.monotonic() < deadline
        reader.step()
        time.sleep(0.01)
    assert sorted(fake.started_for) == ["eng_002", "eng_003"] and fake.read_ids == ["s-eng_001"]
    assert _rows(owner, "select count(*) as n from lakefed_ingest.discovered_table where discovery_id = %s",
                 (did,)) == [{"n": 3}]


def test_a_read_ends_only_by_its_own_statement_and_a_held_claim_keeps_counting(owner):
    store = main.WarehouseReadStore()
    did = _read(owner, ["eng_001"])
    store.claim_reads("sql_a", [did], 8, 8, wr.CLAIM_S)
    store.started(wr.READ, did, "eng_001", "s-new")
    store.finish_read(did, "eng_001", None, "an old statement's answer", "s-old")
    assert _row(owner, did, "eng_001")["status"] == "running"
    _lapse(owner, did, "eng_001")
    assert _going(owner) == 0
    store.hold([(did, "eng_001")])
    assert _going(owner) == 1
    store.finish_read(did, "eng_001", None, "Login failed", "s-new")
    done = _row(owner, did, "eng_001")
    assert (done["status"], done["error"]) == ("failed", "Login failed") and "claimed_at" not in done["listing"]


def test_the_reader_leaves_alone_a_draft_nobody_has_asked_about_and_a_read_too_old(owner):
    stale = _draft(owner, ["eng_001"])
    old = _read(owner, ["eng_002"])
    fresh = _read(owner, ["eng_003"])
    owner.execute("update lakefed_ingest.discovery set started_at = now() - interval '7 hours' where discovery_id = %s",
                  (old,))
    owner.execute("update lakefed_ingest.discovery set started_at = now() - interval '3 hours', listing = listing || "
                  "jsonb_build_object('touched_at', (now() - interval '3 hours')::text) where discovery_id = %s", (stale,))
    work = _rows(owner, q.warehouse_read_work_sql(), (wr.CLAIM_S, br.MAX_AGE_S, 7200))
    assert [w["discovery_id"] for w in work] == [fresh]
    owner.execute(q.draft_touch_sql(), (stale, stale))
    assert {w["discovery_id"] for w in _rows(owner, q.warehouse_read_work_sql(), (wr.CLAIM_S, br.MAX_AGE_S, 7200))} \
        == {fresh, stale}


def test_a_failed_read_through_the_store_is_saved_with_why(owner):
    store = main.WarehouseReadStore()
    did = _read(owner, ["eng_001"])
    fake = _Warehouse({"eng_001": [_failed("Login failed for user 'x'.")]}, in_call=True)
    reader = br.Reader(store, fake, cap=8)
    reader.start_workers()
    reader.step()
    deadline = time.monotonic() + 10
    while store.progress(wr.READ, did)["pending"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert _row(owner, did, "eng_001")["error"] == "Login failed for user 'x'."
