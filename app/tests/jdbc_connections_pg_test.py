"""JDBC connections against a real Postgres, as the app's own Lakebase role (select, insert and update, no delete, as
app_role_pg_test.py sets it up): a connection made through the API is stored with its names and trust as text, a taken
name is refused, and the list reads it back. The asserts read as the owner.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py) where the test may create a
role; skipped, saying so, when it can't. Not run in the local suite: main runs it on the Lakebase branch."""
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
GRANTS = ("grant usage on schema lakefed_ingest to {role}",
          "grant select, insert, update on all tables in schema lakefed_ingest to {role}",
          "grant usage, select on all sequences in schema lakefed_ingest to {role}")
BODY = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example", "src_port": 1433,
        "secret_scope": "lakefed", "secret_key": "orders-{database}", "trust_server_certificate": True}


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_jdbc_test_{uuid.uuid4().hex[:8]}"
    role = f"lakefed_app_jdbc_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    owner = psycopg.connect(target, autocommit=True)
    made_role = False
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            owner.execute(statement)
        try:
            owner.execute(f"create role {role} nologin")
            made_role = True
            owner.execute(f"grant {role} to current_user")
        except psycopg.Error as error:
            pytest.skip(f"can't make a role to test the app's grants with here: {error}".splitlines()[0])
        for grant in GRANTS:
            owner.execute(grant.format(role=role))

        def as_app():
            conn = psycopg.connect(target, autocommit=True)
            conn.execute(f"set role {role}")
            return conn
        monkeypatch.setattr(main.db, "connect", as_app)
        monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        c.role, c.owner = role, owner
        yield c
    finally:
        if made_role:
            owner.execute(f"drop owned by {role}")
        owner.close()
        admin.execute(f"drop database {name} with (force)")
        if made_role:
            admin.execute(f"drop role if exists {role}")
        admin.close()


def test_a_connection_is_stored_with_its_names_and_trust_as_text_and_read_back(client):
    made = client.post("/api/jdbc-connections", json=BODY)
    assert made.status_code == 200, made.text
    assert made.json()["connection"]["trust"] == "trust"
    row = client.owner.execute(
        "select src_type, src_host, src_port, secret_scope, secret_key, trust_server_certificate, created_by "
        "from lakefed_ingest.jdbc_connection where name = %s", ("orders jdbc",)).fetchone()
    assert row == ("sqlserver", "orders.internal.example", 1433, "lakefed", "orders-{database}", "true",
                   "op@example.com")
    listed = client.get("/api/jdbc-connections").json()["connections"]
    assert [c["name"] for c in listed] == ["orders jdbc"] and listed[0]["secret_key"] == "orders-{database}"


def test_a_name_already_taken_is_refused_and_the_first_connection_stays(client):
    assert client.post("/api/jdbc-connections", json=BODY).status_code == 200
    again = client.post("/api/jdbc-connections", json={**BODY, "src_host": "other.example", "trust_server_certificate": False})
    assert again.status_code == 409
    host = client.owner.execute("select src_host, trust_server_certificate from lakefed_ingest.jdbc_connection "
                                "where name = %s", ("orders jdbc",)).fetchone()
    assert host == ("orders.internal.example", "true")

