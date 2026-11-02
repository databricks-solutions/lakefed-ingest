"""A draft: the tables of the databases chosen are read through the warehouse as they are chosen, before the job
exists, and Create takes them (main._draft_reading, create_ingestion_job, _create_after_reading, discard_draft,
_keep_selection). Lakebase, the warehouse and the write are faked; the statements themselves run against Postgres as
the app's own role in app_role_pg_test.py."""
import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

SELECTION = main.Selection(discovery_ids=["d1"], databases=[{"server": "sql_a", "database": "eng_001"}])


def _reads(pending=0, done=2, failed=0, discarded=0, tables=10):
    return {"discovery_id": "d1", "src_connection": "sql_a", "databases": pending + done + failed, "pending": pending,
            "done": done, "failed": failed, "discarded": discarded, "tables": tables}


def _lakebase(monkeypatch, reads, failed_rows=()):
    """db.query answering the draft's statements: its reads (one answer per call, the last repeating) and its failed
    databases; db.execute recorded."""
    reads, done, written = list(reads), [], []

    def query(sql, params=()):
        if sql == q.draft_reads_sql():
            return [reads.pop(0) if len(reads) > 1 else reads[0]]
        if sql == q.draft_failed_sql():
            return list(failed_rows)
        raise AssertionError(f"unexpected query: {sql[:80]}")
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): written.append((sql, params)))
    monkeypatch.setattr(main.warehouse_reads, "advance",
                        lambda store, warehouse, kind, did, connection, first=(): done.append((kind, did, connection)))
    return done, written


def test_a_read_still_going_is_stepped_and_its_draft_kept_wanted(monkeypatch):
    advanced, written = _lakebase(monkeypatch, [_reads(pending=3, done=1), _reads(pending=2, done=2)])
    reading = main._draft_reading(SELECTION)
    assert advanced == [("read", "d1", "sql_a")]
    assert written == [(q.draft_touch_sql(), ("d1", "d1"))]
    assert reading["pending"] == 2 and reading["done"] == 2 and reading["failed_databases"] == []


def test_a_choice_read_to_the_end_has_nothing_to_wait_for(monkeypatch):
    advanced, _ = _lakebase(monkeypatch, [_reads()])
    assert main._draft_reading(SELECTION) is None and advanced == []
    assert main._draft_reading(main.Selection(upload_id="u1", whole_server=True)) is None


def test_a_whole_server_read_leaves_out_a_database_the_login_cant_open_and_names_any_other_failure(monkeypatch):
    rows = [{"src_database": "eng_007", "error": "Cannot open database \"eng_007\" requested by the login.",
             "whole_server": True},
            {"src_database": "eng_009", "error": "Login failed for user 'reader'.", "whole_server": False},
            {"src_database": "eng_011", "error": "Query timed out\nmore", "whole_server": True}]
    _lakebase(monkeypatch, [_reads(failed=3)], rows)
    reading = main._draft_reading(SELECTION)
    assert reading["left_out"] == 1 and reading["failed"] == 2
    assert reading["failed_databases"] == [["eng_009", "Login failed for user 'reader'."], ["eng_011", "Query timed out"]]
    assert main._draft_problems(reading) == ["eng_009: couldn't be read: Login failed for user 'reader'.",
                                             "eng_011: couldn't be read: Query timed out"]


def test_a_discarded_draft_cant_be_created_from(monkeypatch):
    _lakebase(monkeypatch, [_reads(discarded=2)])
    assert "discarded" in main._draft_problems(main._draft_reading(SELECTION))[0]


@pytest.fixture(autouse=True)
def _no_database_lines(monkeypatch):
    """A choice by reference to discoveries has no CSV lines naming only a database (they would be read by name)."""
    monkeypatch.setattr(main, "_has_database_lines", lambda source: False)


def _body(dry_run):
    return main.NewJobRequest(name="J", source=SELECTION, dry_run=dry_run, request_id="r-1",
                              settings={"sink_catalog": "main", "sink_schema": "eng", "sink_table": "{table}"})


def test_create_stops_before_anything_is_written_when_a_database_couldnt_be_read(monkeypatch):
    monkeypatch.setattr(main, "_draft_reading", lambda selection, advance=True: {
        **_reads(failed=1), "left_out": 0, "failed_databases": [["eng_009", "Login failed"]]})
    monkeypatch.setattr(main, "_create_now", lambda *a, **k: pytest.fail("Create wrote"))
    with pytest.raises(HTTPException) as refused:
        main.create_ingestion_job(_body(False), request=None)
    assert refused.value.status_code == 422 and refused.value.detail == {"errors": ["eng_009: couldn't be read: Login failed"]}


def test_a_preview_counts_what_is_read_so_far_and_says_how_far_the_read_is(monkeypatch):
    reading = {**_reads(pending=4, done=6, tables=600), "left_out": 0, "failed_databases": []}
    monkeypatch.setattr(main, "_draft_reading", lambda selection, advance=True: reading)
    monkeypatch.setattr(main, "_create_now", lambda body, request, inline=None: {"summary": {"tables": 600}})
    assert main.create_ingestion_job(_body(True), request=None) == {"summary": {"tables": 600}, "reading": reading}


def test_create_waits_for_the_read_in_a_task_then_writes_in_it(monkeypatch):
    readings = [{**_reads(pending=2), "left_out": 0, "failed_databases": []},
                {**_reads(pending=1, done=3), "left_out": 0, "failed_databases": []}, None]
    monkeypatch.setattr(main, "_draft_reading", lambda selection, advance=True: readings.pop(0))
    monkeypatch.setattr(main, "_user", lambda request: "op@example.com")
    monkeypatch.setattr(main, "DRAFT_POLL_S", 0)
    progress, written = [], []
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): progress.append(params[0]))
    monkeypatch.setattr(main, "_create_now", lambda body, request, inline=None: written.append(inline) or {"created": True, "id": 7})

    def task(kind, request_id, job_id, total, user, write, after, background=False):
        assert (kind, request_id, background) == ("create", "r-1", True)
        return write(lambda n: None, 42)
    monkeypatch.setattr(main, "_write_now_or_later", task)
    assert main.create_ingestion_job(_body(False), request=None) == {"created": True, "id": 7}
    assert len(written) == 1 and written[0][1] == 42                 # written once, in the waiting task
    # The Create's own look counts as the first step; the task then reports each step until none is left.
    assert progress == ['{"stage": "reading", "databases": 4, "read": 3, "tables": 10}', '{"stage": "writing"}']


def test_discarding_a_draft_cancels_its_statements_still_going(monkeypatch):
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"statement_id": "s-1"}, {"statement_id": None},
                                                                    {"statement_id": "s-2"}])
    cancelled = []

    def cancel(statement_id):
        cancelled.append(statement_id)
        if statement_id == "s-2":
            raise RuntimeError("the statement has ended")
    monkeypatch.setattr(main.warehouse, "cancel", cancel)
    assert main.discard_draft("d1") == {"discovery_id": "d1", "cancelled": 2} and cancelled == ["s-1", "s-2"]


def test_the_jobs_transaction_takes_the_drafts_it_was_chosen_from():
    sent = []

    class Cursor:
        def execute(self, sql, params=()):
            sent.append((sql, params))
    main._keep_selection(Cursor(), 7, {"parts": [], "kind": "discovery", "source_id": "d1,d2"}, "op")
    assert sent == [(q.draft_claim_sql(), (7, ["d1", "d2"]))]
    sent.clear()
    main._keep_selection(Cursor(), 7, {"parts": [], "kind": "upload", "source_id": "u1"}, "op")
    assert sent == []


def test_the_drafts_statements_take_the_parameters_passed():
    marks = lambda sql: sql.count("%s")
    assert marks(q.draft_of_sql()) == 1 and marks(q.draft_touch_sql()) == 2 and marks(q.draft_discard_sql()) == 1
    assert marks(q.draft_claim_sql()) == 2 and marks(q.draft_reads_sql()) == 1 and marks(q.draft_failed_sql()) == 1
    assert "not (coalesce(listing, '{}'::jsonb) ? 'discarded')" in q.warehouse_claim_sql("read")
