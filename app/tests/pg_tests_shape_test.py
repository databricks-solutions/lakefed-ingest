"""The Postgres tests run only in Databricks (a Lakebase branch), so three mistakes they made there are checked here,
on their text, where every test run sees them:
- a second connection opened from ``conn.info.dsn``, which psycopg returns without the password (a local Postgres
  with trust login takes it; Lakebase refuses it): connect from the test's own address instead;
- an upload posted without the connection it reads through, which the app now requires;
- a job page read without the run poller's table, so the run phase falls back to the Jobs API, which tests never
  reach: the Postgres tests have that table, so they say so (run_poller.table_ready)."""
import pathlib
import re

TESTS = pathlib.Path(__file__).resolve().parent
PG_TESTS = sorted(TESTS.glob("*_pg_test.py"))
UPLOAD = re.compile(r'post\("/api/uploads", json=\{([^}]*)\}')
JOB_PAGE = re.compile(r'get\(f"/api/ingestion-jobs/\{[^}]+\}"')


def test_there_are_postgres_tests_to_check():
    assert len(PG_TESTS) >= 10


def test_no_postgres_test_connects_from_a_connections_dsn():
    assert [path.name for path in PG_TESTS if "info.dsn" in path.read_text()] == []


def test_every_upload_a_postgres_test_posts_names_its_connection():
    missing = [f"{path.name}: {{{body}}}" for path in PG_TESTS for body in UPLOAD.findall(path.read_text())
               if '"connection"' not in body]
    assert missing == []


def test_a_postgres_test_that_reads_a_job_page_reads_its_runs_from_the_poller_table():
    missing = [path.name for path in PG_TESTS
               if JOB_PAGE.search(path.read_text()) and "table_ready" not in path.read_text()]
    assert missing == []
