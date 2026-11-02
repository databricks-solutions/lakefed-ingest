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
    assert _marks(q.warehouse_check_done_sql()) == 5 and _marks(q.warehouse_read_done_sql()) == 5
    assert _marks(q.warehouse_read_rows_sql()) == 5 and _marks(q.warehouse_left_out_sql()) == 2
    assert _marks(q.warehouse_named_rows_sql(3)) == 15
    assert _marks(q.insert_discovered_tables_sql(wr.TABLE_COLUMNS, 2)) == 2 * len(wr.TABLE_COLUMNS)


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
    assert q.warehouse_read_done_sql().endswith("and status = 'running'")
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
        def execute(self, sql, params=()):
            sent.append(sql)

    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor())))
    row = tuple(None for _ in wr.TABLE_COLUMNS)
    main.WarehouseReadStore().finish_read("d1", "eng_001", [row, row], None)
    assert sent and not any("delete" in sql.lower() for sql in sent)


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
