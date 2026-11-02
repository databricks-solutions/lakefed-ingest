"""The JDBC login record table, written and read as the app's own Postgres role: saved, then rotated; no delete; no
password column.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server where the test may create and drop its own database and
roles. Written for Databricks (a Lakebase branch run); not run from a laptop."""
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import jdbc_connections as jc
import pytest
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
SCOPE = "lakefed_ingest_jdbc_logins"
COLUMNS = {"id", "connection", "secret_scope", "secret_key", "entered_by", "entered_at", "outcome"}


@pytest.fixture
def app_role(role_database):
    """A connection as the app's role (role_database)."""
    yield role_database.app


@pytest.fixture
def role_database():
    """The schema applied by its owner, and a login role with the grants the app's grants job gives: USAGE on the
    schema, SELECT, INSERT and UPDATE on the record table and on the job tables a save reads, SELECT and INSERT on the
    connection table, and no DELETE. Yields the app role's connection (app), the owner's (owner), and a way to open
    another session as the app role (connect)."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_jdbc_{uuid.uuid4().hex[:8]}"
    role = f"{name}_app"
    password = uuid.uuid4().hex
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    owner = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    app = None
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            owner.execute(statement)
        owner.execute(sql.SQL("create role {} login password {}").format(sql.Identifier(role), sql.Literal(password)))
        owner.execute(sql.SQL("grant usage on schema lakefed_ingest to {}").format(sql.Identifier(role)))
        owner.execute(sql.SQL("grant select, insert, update on lakefed_ingest.jdbc_login_write to {}")
                      .format(sql.Identifier(role)))
        owner.execute(sql.SQL("grant select, insert on lakefed_ingest.jdbc_connection to {}")
                      .format(sql.Identifier(role)))
        owner.execute(sql.SQL("grant select, insert, update on lakefed_ingest.schedule, lakefed_ingest.schedule_member, "
                              "lakefed_ingest.job_selection, lakefed_ingest.discovery to {}").format(sql.Identifier(role)))
        as_app = psycopg.conninfo.make_conninfo(URI, dbname=name, user=role, password=password)
        app = psycopg.connect(as_app, autocommit=True)
        yield SimpleNamespace(app=app, owner=owner, connect=lambda: psycopg.connect(as_app, autocommit=True))
    finally:
        if app is not None:
            app.close()
        owner.close()
        admin.execute(f"drop database {name}")
        admin.execute(sql.SQL("drop role {}").format(sql.Identifier(role)))
        admin.close()


def test_the_app_role_records_a_saved_login_and_then_a_rotated_one(app_role):
    assert _record(app_role, "orders-db", "ann@example.com") == "saved"
    assert _record(app_role, "orders-db", "ben@example.com") == "rotated"


def test_a_login_is_told_apart_by_its_scope_and_key(app_role):
    _record(app_role, "orders-db", "ann@example.com")
    assert _record(app_role, "billing-db", "ann@example.com") == "saved"


def test_the_newest_record_for_a_key_names_who_rotated_it(app_role):
    _record(app_role, "orders-db", "ann@example.com")
    _record(app_role, "orders-db", "ben@example.com")
    with app_role.cursor() as cursor:
        cursor.execute(q.jdbc_login_latest_sql(), (SCOPE, "orders-db"))
        row = dict(zip([d.name for d in cursor.description], cursor.fetchone()))
    assert row["entered_by"] == "ben@example.com" and row["outcome"] == "rotated"


def test_the_app_role_cannot_delete_a_login_record(app_role):
    import psycopg
    _record(app_role, "orders-db", "ann@example.com")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_role.execute("delete from lakefed_ingest.jdbc_login_write")


def test_the_record_table_has_no_column_for_a_password_or_secret_value(app_role):
    with app_role.cursor() as cursor:
        cursor.execute("select column_name from information_schema.columns "
                       "where table_schema = 'lakefed_ingest' and table_name = 'jdbc_login_write'")
        columns = {row[0] for row in cursor.fetchall()}
    assert columns == COLUMNS


def test_a_connection_is_backed_by_a_login_its_maker_typed_under_its_name_and_key(app_role):
    _record(app_role, "orders-login", "ann@example.com", connection_name="orders-sql")
    assert _backed(app_role, "orders-sql", "orders-login", "ann@example.com") is True


def test_a_login_typed_by_someone_else_does_not_back_the_connection(app_role):
    _record(app_role, "orders-login", "ben@example.com", connection_name="orders-sql")
    assert _backed(app_role, "orders-sql", "orders-login", "ann@example.com") is False


def test_a_login_under_another_connection_name_or_another_key_does_not_back_it(app_role):
    _record(app_role, "orders-login", "ann@example.com", connection_name="billing-sql")
    _record(app_role, "orders-login-2", "ann@example.com", connection_name="orders-sql")
    assert _backed(app_role, "orders-sql", "orders-login", "ann@example.com") is False


def test_a_database_template_key_is_matched_by_each_database_key_it_stands_for(app_role):
    _record(app_role, "creds-orders-db", "ann@example.com", connection_name="orders-sql")
    _record(app_role, "creds-billing-db", "ann@example.com", connection_name="orders-sql")
    assert _backed(app_role, "orders-sql", "creds-{database}", "ann@example.com") is True
    assert _backed(app_role, "orders-sql", "orders-login", "ann@example.com") is False


def test_the_newest_login_per_key_names_the_connection_it_was_typed_for(app_role):
    _record(app_role, "creds-sales", "ann@example.com", connection_name="orders-sql")
    _record(app_role, "creds-sales", "mallory@example.com", connection_name="evil")
    _record(app_role, "creds-hr", "ann@example.com", connection_name="orders-sql")
    newest = _newest(app_role, ["creds-sales", "creds-hr", "creds-none"])
    assert newest == {"creds-sales": "evil", "creds-hr": "orders-sql"}


def test_the_newest_login_per_key_reads_only_the_scope_it_is_asked_for(app_role):
    with app_role.cursor() as cursor:
        cursor.execute(q.jdbc_login_insert_sql(), (uuid.uuid4().hex, "evil", "other_scope", "creds-sales",
                                                   "mallory@example.com", "other_scope", "creds-sales"))
    _record(app_role, "creds-sales", "ann@example.com", connection_name="orders-sql")
    assert _newest(app_role, ["creds-sales"]) == {"creds-sales": "orders-sql"}


def test_the_writers_of_a_key_are_who_typed_its_logins_and_who_made_their_connections(app_role):
    with app_role.cursor() as cursor:
        cursor.execute(jc.insert_sql(), ("orders-sql", "sqlserver", "orders.example", 1433, SCOPE, "creds-{database}",
                                         "false", "bob@example.com"))
    _record(app_role, "creds-sales", "ann@example.com", connection_name="orders-sql")
    _record(app_role, "creds-sales", "ann@example.com", connection_name="gone")
    _record(app_role, "creds-hr", "eve@example.com", connection_name="orders-sql")
    with app_role.cursor() as cursor:
        cursor.execute(q.jdbc_login_writers_sql(), (SCOPE, "creds-sales"))
        writers = set(cursor.fetchall())
    assert writers == {("ann@example.com", "bob@example.com"), ("ann@example.com", None)}


def test_the_app_role_takes_a_keys_login_lock_and_the_record_commits_only_with_the_put(app_role):
    """The save's transaction, as the route runs it: the key's lock, the record, then the secret write. A write that
    fails rolls the record back; one that succeeds commits it."""
    import psycopg

    def save(put_fails):
        with app_role.transaction():
            with app_role.cursor() as cursor:
                cursor.execute(q.jdbc_login_lock_sql(), (q.jdbc_login_lock_key(SCOPE, "creds-sales"),))
                cursor.execute(q.jdbc_login_insert_sql(), (uuid.uuid4().hex, "orders-sql", SCOPE, "creds-sales",
                                                           "ann@example.com", SCOPE, "creds-sales"))
                if put_fails:
                    raise RuntimeError("the secret write was refused")

    with pytest.raises(RuntimeError):
        save(put_fails=True)
    assert _count(app_role, "creds-sales") == 0
    save(put_fails=False)
    assert _count(app_role, "creds-sales") == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_role.execute("delete from lakefed_ingest.jdbc_login_write")


def test_a_keys_login_lock_makes_a_second_save_wait_for_the_first(role_database):
    """While one save holds the key's lock, another app session can't take it, and can once the first has committed."""
    app, key = role_database.app, q.jdbc_login_lock_key(SCOPE, "creds-sales")
    try_lock = "select pg_try_advisory_xact_lock(hashtextextended(%s, 0))"
    other = role_database.connect()
    try:
        with app.transaction():
            app.execute(q.jdbc_login_lock_sql(), (key,))
            assert other.execute(try_lock, (key,)).fetchone()[0] is False
        assert other.execute(try_lock, (key,)).fetchone()[0] is True
    finally:
        other.close()


def test_the_groups_of_a_connections_jobs_are_read_as_the_app_role(role_database):
    app_role, owner = role_database.app, role_database.owner
    job = owner.execute("insert into lakefed_ingest.schedule (name, cadence_minutes) values ('Orders', 60) "
                        "returning id").fetchone()[0]
    gone = owner.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, deleted_at) values "
                         "('Gone', 60, now()) returning id").fetchone()[0]
    owner.execute("insert into lakefed_ingest.discovery (discovery_id, src_database, status, listing) values "
                  "('d1', 'sales', 'ok', '{\"jdbc_connection\": \"orders-sql\"}'), "
                  "('d2', 'hr', 'ok', '{\"jdbc_connection\": \"orders-sql\"}'), "
                  "('d3', 'hr', 'ok', '{\"jdbc_connection\": \"other\"}')")
    for schedule_id, source_id in ((job, "d1,d2"), (gone, "d1")):
        owner.execute("insert into lakefed_ingest.job_selection (job_id, scope, server, source_kind, source_id) "
                      "values (%s, 'database', 'h1', 'discovery', %s)", (schedule_id, source_id))
    owner.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection) values "
                  "(%s, 'orders_g01'), (%s, 'orders_g02'), (%s, 'gone_g01')", (job, job, gone))
    owner.execute("update lakefed_ingest.schedule_member set removed_at = now() where task_collection = 'orders_g02'")
    with app_role.cursor() as cursor:
        cursor.execute(q.jdbc_connection_groups_sql(), ("orders-sql",))
        assert sorted(row[0] for row in cursor.fetchall()) == ["orders_g01"]
        cursor.execute(q.jdbc_connection_groups_sql(), ("nobody",))
        assert cursor.fetchall() == []


def _count(connection, secret_key):
    return connection.execute("select count(*) from lakefed_ingest.jdbc_login_write where secret_scope = %s "
                              "and secret_key = %s", (SCOPE, secret_key)).fetchone()[0]


def _newest(connection, keys):
    """The connection each key's newest login names, by the per-key query as the app runs it."""
    with connection.cursor() as cursor:
        cursor.execute(q.jdbc_login_latest_per_key_sql(), (SCOPE, keys))
        names = [d.name for d in cursor.description]
        return {row["secret_key"]: row["connection"] for row in (dict(zip(names, r)) for r in cursor.fetchall())}


def _backed(connection, connection_name, key_template, entered_by):
    """Whether the connection-login query, as the app runs it, finds a login this maker typed under this name for a key
    the template makes (jdbc_connections.key_fits, as main._login_typed_for keeps them)."""
    with connection.cursor() as cursor:
        cursor.execute(q.jdbc_connection_login_sql(), (SCOPE, connection_name, entered_by))
        return any(jc.key_fits(key_template, row[0]) for row in cursor.fetchall())


def _record(connection, secret_key, entered_by, connection_name="orders-sql"):
    """One login write's row, as the route inserts it; returns its outcome."""
    entry_id = uuid.uuid4().hex
    with connection.cursor() as cursor:
        cursor.execute(q.jdbc_login_insert_sql(),
                       (entry_id, connection_name, SCOPE, secret_key, entered_by, SCOPE, secret_key))
        return cursor.fetchone()[1]
