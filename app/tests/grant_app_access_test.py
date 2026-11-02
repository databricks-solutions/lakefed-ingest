"""The grant notebook's statements for the app's Lakebase role, read as text: the blanket grant on the schema's tables
is followed by a revoke of UPDATE on the two JDBC record tables, which the app only reads and inserts into."""
import json
from pathlib import Path

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "grant_app_access.ipynb"
BLANKET_GRANT = "grant select, insert, update on all tables in schema lakefed_ingest to {}"
REVOKE = "revoke update on lakefed_ingest.jdbc_login_write, lakefed_ingest.jdbc_connection from {}"


def _grant_source():
    return "".join(json.loads(NOTEBOOK.read_text())["cells"][1]["source"])


def test_the_app_loses_update_on_the_two_jdbc_record_tables_after_the_blanket_grant():
    source = _grant_source()
    assert source.count(REVOKE) == 1
    assert source.index(BLANKET_GRANT) < source.index(REVOKE)


def test_the_revoke_names_only_the_two_jdbc_record_tables():
    revokes = [line for line in _grant_source().splitlines() if line.lstrip().startswith('sql.SQL("revoke')]
    assert revokes == [f'    sql.SQL("{REVOKE}").format(r),']
