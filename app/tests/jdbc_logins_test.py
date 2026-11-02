"""The JDBC login write path: the app writes a typed login into the dedicated secret scope as its own service principal,
records who entered it, and never lets the password reach a log, an error, a response or a 422.

Plain function tests: the Secrets API, Lakebase and the permission checks are stubbed, so no workspace or database is
reached (conftest refuses a workspace call a test forgot to stub)."""
import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import SecretStr, ValidationError

import jdbc_connections as jc
import jdbc_logins as jl
import lakebase
import main
import queries as q

PASSWORD = 'hunter2-"quoted"\\slash-ZX81'
SCOPE = "lakefed_ingest_jdbc_logins"
ENTERER = "ann@example.com"
REAL_GROUPS_REFUSAL = main._groups_refusal                   # the world fixture stubs it; these tests put it back


class FakeSecrets:
    def __init__(self, fail_with=None, events=None):
        self.calls = []
        self.fail_with = fail_with
        self.events = events if events is not None else []

    def put_secret(self, scope, key, *, bytes_value=None, string_value=None):
        self.events.append("put")
        self.calls.append({"scope": scope, "key": key, "string_value": string_value, "bytes_value": bytes_value})
        if self.fail_with is not None:
            raise self.fail_with


class FakeWorkspace:
    def __init__(self, secrets):
        self.secrets = secrets


class FakeLakebase:
    """Stands in for db.query: the control rows of one key's groups and the job holding them (it reads the login scope
    unless job_scope says otherwise), the login rows, who made each connection, and the insert's outcome."""

    def __init__(self, groups=("group-a",), fail_insert=None, job_scope=SCOPE):
        self.groups = list(groups)
        self.fail_insert = fail_insert
        self.job_scope = job_scope
        self.creators = {}
        self.connection_groups = {}
        self.logins = []                # committed
        self.pending = []               # inserted in the open transaction
        self.statements = []
        self.events = []                # lock, insert, put, commit or rollback, in order

    @contextlib.contextmanager
    def transaction(self):
        self.pending = []
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            self.pending = []
            raise
        self.logins += self.pending
        self.pending = []
        self.events.append("commit")

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql == q.jdbc_login_lock_sql():
            self.events.append(("lock", params[0]))

    def fetch(self, sql, params=()):
        if sql == q.jdbc_login_insert_sql():
            self.events.append("insert")
        return self.query(sql, params)

    def query(self, sql, params=()):
        self.statements.append((sql, params))
        if sql == q.control_groups_for_secret_key_sql():
            return [{"task_collection": g} for g in self.groups]
        if sql == q.schedules_sql():
            return [{"id": 5, "name": "orders", "parameters": json.dumps({"secret_scope": self.job_scope})}]
        if sql == q.schedule_members_sql():
            return [{"schedule_id": 5, "task_collection": g, "removed_at": None} for g in self.groups if 5 in params[0]]
        if sql == q.jdbc_connection_groups_sql():
            return [{"task_collection": g} for g in self.connection_groups.get(params[0], [])]
        if sql == jc.get_sql():
            return [{"name": params[0], "created_by": self.creators[params[0]]}] if params[0] in self.creators else []
        if sql == q.jdbc_login_writers_sql():
            scope, key = params
            return [{"entered_by": l["entered_by"], "created_by": self.creators.get(l["connection"])}
                    for l in self.logins if l["scope"] == scope and l["key"] == key]
        if sql == q.jdbc_login_insert_sql():
            if self.fail_insert is not None:
                raise self.fail_insert
            entry_id, connection, scope, key, entered_by = params[:5]
            outcome = "rotated" if any(l["scope"] == scope and l["key"] == key for l in self.logins) else "saved"
            entry = {"id": entry_id, "connection": connection, "scope": scope, "key": key, "entered_by": entered_by,
                     "outcome": outcome}
            self.pending.append(entry)
            return [{"id": entry_id, "outcome": outcome, "entered_at": "2026-10-30T16:00:00+00:00"}]
        if sql == q.jdbc_login_latest_sql():
            scope, key = params
            found = [l for l in self.logins if l["scope"] == scope and l["key"] == key]
            return [dict(found[-1], entered_at="2026-10-30T16:00:00+00:00")] if found else []
        raise AssertionError(f"unexpected statement: {sql[:60]}")


def _request(entered_by=ENTERER):
    return SimpleNamespace(headers={"X-Forwarded-Email": entered_by})


def _job_reads_key(database):
    """db.query where one live job, orders, holds group-a, the group that reads the key (the fake database's answer)."""
    def query(sql, params=()):
        if sql == q.schedules_sql():
            return [{"id": 5, "name": "orders", "parameters": {"secret_scope": SCOPE}}]
        if sql == q.schedule_members_sql():
            return [{"schedule_id": 5, "task_collection": "group-a", "removed_at": None}]
        return database.query(sql, params)
    return query


def _body(**changes):
    body = {"connection": "orders-sql", "secret_key": "orders-db", "user_name": "loader", "password": PASSWORD}
    body.update(changes)
    return jl.JdbcLoginRequest(**body)


@pytest.fixture
def world(monkeypatch):
    """The app with a dedicated scope, a fake client and a fake database. Nothing is written for real."""
    monkeypatch.setenv(jl.SCOPE_ENV, SCOPE)
    database = FakeLakebase()
    secrets = FakeSecrets(events=database.events)
    monkeypatch.setattr(lakebase, "workspace", lambda: FakeWorkspace(secrets))
    monkeypatch.setattr(main.db, "query", database.query)
    monkeypatch.setattr(main.db, "transaction", database.transaction)
    monkeypatch.setattr(main.db, "fetch", lambda cur, sql, params=(): cur.fetch(sql, params))
    monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: None)
    return SimpleNamespace(secrets=secrets, database=database, monkeypatch=monkeypatch)


# ---- the request model ----------------------------------------------------------------------------

def test_the_password_is_a_secret_and_its_repr_and_str_hide_it():
    body = _body()
    assert isinstance(body.password, SecretStr)
    assert PASSWORD not in repr(body) and PASSWORD not in str(body)
    assert PASSWORD not in repr(body.password) and PASSWORD not in str(body.password)
    assert body.password.get_secret_value() == PASSWORD


def test_the_request_names_no_scope_and_refuses_one(world):
    with pytest.raises(ValidationError):
        jl.JdbcLoginRequest(connection="orders-sql", secret_key="orders-db", user_name="loader", password=PASSWORD,
                            secret_scope="another-scope")


@pytest.mark.parametrize("bad_key", ["../other", "a/b", "with space", "", "x" * 129, "a.b", "a_b"])
def test_a_secret_key_is_a_plain_name(bad_key):
    with pytest.raises(ValidationError):
        _body(secret_key=bad_key)


def test_a_login_key_takes_the_same_letters_as_a_connection_key():
    assert _body(secret_key="orders-login").resolved_secret_key() == "orders-login"
    assert _body(secret_key="x" * 127).resolved_secret_key() == "x" * 127
    with pytest.raises(ValidationError):
        _body(secret_key="x" * 128)


def test_a_key_with_database_in_it_needs_the_database_its_login_is_for():
    with pytest.raises(ValidationError) as refused:
        _body(secret_key="creds-{database}")
    assert any(item["loc"] == ("database",) for item in refused.value.errors())


@pytest.mark.parametrize("bad_database", ["sales db", "sales/db", "  ", "x" * 123])
def test_a_database_name_that_cannot_fill_the_key_is_refused(bad_database):
    with pytest.raises(ValidationError):
        _body(secret_key="creds-{database}", database=bad_database)


@pytest.mark.parametrize("database", ["Sales_DB", "sales.eu", "sales-2"])
def test_a_database_name_with_underscores_or_dots_fills_the_key_discovery_reads(database):
    """discovery.secret_key_for puts the raw name in place of {database}; the login goes under that same key."""
    assert _body(secret_key="creds-{database}", database=database).resolved_secret_key() == f"creds-{database}"


def test_a_database_name_with_a_space_says_why_it_cannot_have_a_login():
    with pytest.raises(ValidationError) as refused:
        _body(secret_key="creds-{database}", database="sales db")
    assert "a space" in str(refused.value)


def test_a_key_with_no_database_in_it_takes_no_database():
    with pytest.raises(ValidationError):
        _body(database="sales")
    assert _body(database="").resolved_secret_key() == "orders-db"


def test_a_database_fills_the_key_the_login_is_written_under(world):
    out = main.write_jdbc_login(_body(secret_key="creds-{database}", database="sales"), _request())
    assert out["secret_key"] == "creds-sales" and world.secrets.calls[0]["key"] == "creds-sales"
    assert world.database.logins[0]["key"] == "creds-sales"


def test_the_groups_checked_are_the_ones_that_read_the_resolved_key(world):
    main.write_jdbc_login(_body(secret_key="creds-{database}", database="sales"), _request())
    assert (q.control_groups_for_secret_key_sql(), ("creds-sales",)) in world.database.statements


def test_the_scope_is_read_without_naming_a_key(world):
    assert main.last_jdbc_login("") == {"scope": SCOPE, "secret_key": "", "login": None}
    assert world.database.statements == []


@pytest.mark.parametrize("bad_password", ["", "x" * (jl.PASSWORD_MAX + 1)])
def test_a_password_has_a_size_and_nothing_else_checked(bad_password):
    with pytest.raises(ValidationError):
        _body(password=bad_password)


# ---- the secret value and redaction --------------------------------------------------------------

def test_the_secret_is_the_json_the_engine_reads():
    value = jl.secret_value("loader", SecretStr(PASSWORD))
    assert json.loads(value) == {"user": "loader", "password": PASSWORD}
    assert "\n" not in value


def test_redact_hides_the_plain_and_the_json_escaped_password():
    message = f"bad value {PASSWORD} and {json.dumps(PASSWORD)[1:-1]} here"
    redacted = jl.redact(message, PASSWORD)
    assert PASSWORD not in redacted
    assert json.dumps(PASSWORD)[1:-1] not in redacted
    assert redacted.count(jl.REDACTED) == 2


# ---- the write goes through the app's own identity ----------------------------------------------

def test_the_login_is_written_as_the_apps_own_client_with_the_scope_key_and_json(world):
    lakebase_client = []
    world.monkeypatch.setattr(lakebase, "workspace", lambda: lakebase_client.append(1) or FakeWorkspace(world.secrets))
    main.write_jdbc_login(_body(), _request())
    assert lakebase_client == [1]
    assert world.secrets.calls == [{"scope": SCOPE, "key": "orders-db", "bytes_value": None,
                                    "string_value": jl.secret_value("loader", SecretStr(PASSWORD))}]


def test_the_write_module_takes_no_users_token():
    source = (Path(jl.__file__)).read_text()
    assert "X-Forwarded-Access-Token" not in source
    assert "WorkspaceClient(" not in source


def test_the_response_names_the_scope_and_key_and_never_the_password(world):
    out = main.write_jdbc_login(_body(), _request())
    assert out["scope"] == SCOPE and out["secret_key"] == "orders-db" and out["connection"] == "orders-sql"
    assert out["entered_by"] == ENTERER and out["outcome"] == "saved"
    assert out["entered_at"] and out["id"]
    assert PASSWORD not in json.dumps(out)
    assert "loader" not in json.dumps(out)


def test_writing_a_key_again_rewrites_it_and_records_rotated(world):
    main.write_jdbc_login(_body(), _request("ann@example.com"))
    second = main.write_jdbc_login(_body(password="a-new-password"), _request("ben@example.com"))
    assert second["outcome"] == "rotated"
    assert second["entered_by"] == "ben@example.com"
    assert len(world.secrets.calls) == 2
    assert json.loads(world.secrets.calls[1]["string_value"])["password"] == "a-new-password"


def test_the_latest_login_for_a_key_is_read_back_without_the_password(world):
    main.write_jdbc_login(_body(), _request())
    out = main.last_jdbc_login("orders-db")
    assert out["scope"] == SCOPE and out["login"]["entered_by"] == ENTERER
    assert PASSWORD not in json.dumps(out)


def test_the_scope_must_be_set(world):
    world.monkeypatch.delenv(jl.SCOPE_ENV)
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request())
    assert refused.value.status_code == 503
    assert world.secrets.calls == []


def test_the_record_is_an_insert_and_never_a_delete():
    sql = q.jdbc_login_insert_sql().lower()
    assert sql.startswith("insert into lakefed_ingest.jdbc_login_write")
    assert "delete" not in sql and "update" not in sql
    assert "'rotated'" in sql and "'saved'" in sql


# ---- permission ---------------------------------------------------------------------------------

def test_a_user_who_may_not_change_the_groups_is_refused_before_anything_is_written(world):
    world.monkeypatch.setattr(main, "_groups_refusal",
                              lambda groups, user: f"these tables are in the ingestion job orders: {user} can't manage it")
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request("eve@example.com"))
    assert refused.value.status_code == 403
    assert "eve@example.com" in refused.value.detail
    assert world.secrets.calls == []
    assert world.database.logins == []


def test_the_groups_checked_are_the_ones_that_read_the_key(world):
    seen = []
    world.monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: seen.append(sorted(groups)) or None)
    main.write_jdbc_login(_body(), _request())
    assert seen == [["group-a"]]


def test_a_new_key_no_job_reads_is_open_to_any_signed_in_user(world):
    world.monkeypatch.setattr(main, "_groups_refusal", REAL_GROUPS_REFUSAL)
    world.monkeypatch.setattr(main.db, "query", lambda sql, params=(): [] if sql == q.schedules_sql()
                              else world.database.query(sql, params))
    result = main.write_jdbc_login(_body(), _request("eve@example.com"))
    assert result["outcome"] == "saved" and world.secrets.calls[0]["key"] == "orders-db"


def _no_job_reads_the_key(world):
    world.monkeypatch.setattr(main, "_groups_refusal", REAL_GROUPS_REFUSAL)
    world.database.groups = []


def test_a_login_no_job_reads_yet_is_replaced_only_by_who_typed_it(world):
    _no_job_reads_the_key(world)
    assert main.write_jdbc_login(_body(), _request("ann@example.com"))["outcome"] == "saved"
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request("eve@example.com"))
    assert refused.value.status_code == 403 and "already holds a login" in refused.value.detail
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(connection="evil"), _request("eve@example.com"))
    assert refused.value.status_code == 403 and "typed for the JDBC connection orders-sql" in refused.value.detail
    assert len(world.secrets.calls) == 1 and len(world.database.logins) == 1
    assert main.write_jdbc_login(_body(), _request("Ann@Example.com"))["outcome"] == "rotated"


def test_the_maker_of_the_connection_a_login_was_typed_for_may_replace_it(world):
    _no_job_reads_the_key(world)
    world.database.creators["orders-sql"] = "bob@example.com"
    main.write_jdbc_login(_body(), _request("ann@example.com"))
    assert main.write_jdbc_login(_body(), _request("bob@example.com"))["outcome"] == "rotated"


def test_a_job_reading_the_key_from_another_scope_does_not_gate_the_login_scopes_key(world):
    world.database.job_scope = "lakefed"
    seen = []
    world.monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: seen.append(sorted(groups)) or "refused")
    assert main.write_jdbc_login(_body(), _request("eve@example.com"))["outcome"] == "saved"
    assert seen == []


def test_a_job_reading_the_key_from_the_login_scope_gates_it_even_for_who_typed_it(world):
    seen = []
    world.monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: seen.append(sorted(groups)) or "no")
    with pytest.raises(main.HTTPException):
        main.write_jdbc_login(_body(), _request("ann@example.com"))
    assert seen == [["group-a"]] and world.secrets.calls == []


def test_a_call_with_no_signed_in_user_writes_nothing_even_for_a_new_key(world):
    world.monkeypatch.setattr(main, "_groups_refusal", REAL_GROUPS_REFUSAL)
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request(""))
    assert refused.value.status_code == 403 and refused.value.detail == main.NO_IDENTITY
    assert world.secrets.calls == [] and world.database.logins == []


def test_a_user_without_rights_on_the_job_that_reads_the_key_cannot_replace_its_login(world):
    world.monkeypatch.setattr(main, "_groups_refusal", REAL_GROUPS_REFUSAL)
    world.monkeypatch.setattr(main.db, "query", _job_reads_key(world.database))
    world.monkeypatch.setattr(main, "_permission_refusals",
                              lambda job, user: {"change": f"{user} has no Can Manage on the job"})
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request("eve@example.com"))
    assert refused.value.status_code == 403
    assert refused.value.detail == ("these tables are in the ingestion job orders: "
                                    "eve@example.com has no Can Manage on the job")
    assert world.secrets.calls == [] and world.database.logins == []


def test_a_user_with_rights_on_that_job_replaces_its_login(world):
    world.monkeypatch.setattr(main, "_groups_refusal", REAL_GROUPS_REFUSAL)
    world.monkeypatch.setattr(main.db, "query", _job_reads_key(world.database))
    world.monkeypatch.setattr(main, "_permission_refusals", lambda job, user: {"change": None})
    assert main.write_jdbc_login(_body(), _request("ann@example.com"))["outcome"] == "saved"
    assert world.secrets.calls[0]["key"] == "orders-db"


def test_a_write_refused_by_databricks_says_so_and_keeps_the_password_out(world):
    class Refused(Exception):
        status_code = 403

    world.secrets.fail_with = Refused(f"permission denied for {PASSWORD}")
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request())
    assert refused.value.status_code == 403
    assert "WRITE" in refused.value.detail
    assert PASSWORD not in refused.value.detail


def test_any_other_failed_write_is_redacted_and_says_it_may_not_be_saved(world):
    world.secrets.fail_with = RuntimeError(f"gateway said {json.dumps(PASSWORD)[1:-1]} and {PASSWORD}")
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request())
    assert refused.value.status_code == 502
    assert PASSWORD not in refused.value.detail
    assert json.dumps(PASSWORD)[1:-1] not in refused.value.detail
    assert "may not be saved" in refused.value.detail


def test_a_failed_record_writes_no_secret_and_redacts(world):
    world.database.fail_insert = RuntimeError(f"no {PASSWORD}")
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request())
    assert refused.value.status_code == 500
    assert "nothing was saved" in refused.value.detail and PASSWORD not in refused.value.detail
    assert world.secrets.calls == [] and world.database.logins == []


# ---- one transaction under the key's lock (N5) ----------------------------------------------------

def test_a_save_locks_the_key_records_writes_the_secret_then_commits(world):
    main.write_jdbc_login(_body(), _request())
    assert world.database.events == [("lock", q.jdbc_login_lock_key(SCOPE, "orders-db")), "insert", "put", "commit"]
    assert len(world.database.logins) == 1


def test_a_refused_secret_write_rolls_the_record_back(world):
    world.secrets.fail_with = RuntimeError("gateway timeout")
    with pytest.raises(main.HTTPException):
        main.write_jdbc_login(_body(), _request())
    assert world.database.events[-3:] == ["insert", "put", "rollback"]
    assert world.database.logins == []


def test_a_save_that_finds_another_login_saved_meanwhile_is_refused_and_writes_nothing(world):
    main.write_jdbc_login(_body(), _request())
    world.monkeypatch.setattr(main, "_newest_login", lambda scope, key: None)    # read before the other save landed
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request())
    assert refused.value.status_code == 409 and "meanwhile" in refused.value.detail
    assert len(world.secrets.calls) == 1 and len(world.database.logins) == 1


def test_a_commit_that_fails_after_the_secret_is_written_says_to_save_again(world):
    def failing_commit():
        @contextlib.contextmanager
        def transaction():
            with world.database.transaction() as cur:
                yield cur
            raise RuntimeError(f"connection lost {PASSWORD}")
        return transaction
    world.monkeypatch.setattr(main.db, "transaction", failing_commit())
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(), _request())
    assert refused.value.status_code == 500 and "save it again" in refused.value.detail
    assert PASSWORD not in refused.value.detail


def test_the_lock_key_names_the_scope_and_the_key_and_no_collection_lock():
    assert q.jdbc_login_lock_key(SCOPE, "creds-sales") == f"lakefed_ingest:jdbc_login:{SCOPE}/creds-sales"
    assert "pg_advisory_xact_lock" in q.jdbc_login_lock_sql()


# ---- a key whose newest login was typed for another connection (N5) -------------------------------

def _no_job_reads(world):
    world.monkeypatch.setattr(main, "_groups_refusal", REAL_GROUPS_REFUSAL)
    world.database.groups = []


def test_a_save_under_another_connection_is_refused_unless_the_user_may_change_its_jobs(world):
    _no_job_reads(world)
    main.write_jdbc_login(_body(connection="orders-sql"), _request("ann@example.com"))
    world.database.connection_groups["orders-sql"] = ["orders_g01"]
    world.monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: None if user == "ops@example.com" else "no")
    with pytest.raises(main.HTTPException) as refused:
        main.write_jdbc_login(_body(connection="evil"), _request("ann@example.com"))
    assert refused.value.status_code == 403 and "typed for the JDBC connection orders-sql" in refused.value.detail
    assert main.write_jdbc_login(_body(connection="evil"), _request("ops@example.com"))["outcome"] == "rotated"


def test_a_save_under_another_connection_with_no_jobs_is_its_typers_or_its_makers(world):
    _no_job_reads(world)
    world.database.creators["orders-sql"] = "bob@example.com"
    main.write_jdbc_login(_body(connection="orders-sql"), _request("ann@example.com"))
    with pytest.raises(main.HTTPException):
        main.write_jdbc_login(_body(connection="evil"), _request("eve@example.com"))
    assert main.write_jdbc_login(_body(connection="orders-two"), _request("ann@example.com"))["outcome"] == "rotated"
    with pytest.raises(main.HTTPException):         # ann's newest login now names orders-two, which bob didn't make
        main.write_jdbc_login(_body(connection="orders-three"), _request("bob@example.com"))


def test_whoever_may_change_the_connections_jobs_may_replace_its_login(world):
    _no_job_reads(world)
    main.write_jdbc_login(_body(), _request("ann@example.com"))
    world.database.connection_groups["orders-sql"] = ["orders_g01"]
    world.monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: None)
    assert main.write_jdbc_login(_body(), _request("ops@example.com"))["outcome"] == "rotated"


# ---- no password in a log, an error, or a 422 --------------------------------------------------

def test_no_log_line_carries_the_password_on_success_rotation_or_refusal(world, caplog):
    caplog.set_level(logging.DEBUG)
    main.write_jdbc_login(_body(), _request())
    main.write_jdbc_login(_body(), _request())
    world.secrets.fail_with = RuntimeError(f"bad {PASSWORD}")
    with pytest.raises(main.HTTPException):
        main.write_jdbc_login(_body(), _request())
    assert "jdbc login saved" in caplog.text and "jdbc login rotated" in caplog.text
    assert PASSWORD not in caplog.text
    assert json.dumps(PASSWORD)[1:-1] not in caplog.text
    for record in caplog.records:
        assert PASSWORD not in record.getMessage()


def test_no_log_line_carries_the_password_when_the_record_fails(world, caplog):
    caplog.set_level(logging.DEBUG)
    world.database.fail_insert = RuntimeError(f"no {PASSWORD}")
    with pytest.raises(main.HTTPException):
        main.write_jdbc_login(_body(), _request())
    assert PASSWORD not in caplog.text


def test_the_validation_error_for_a_password_keeps_where_and_why_but_not_the_input():
    too_long = PASSWORD + "x" * jl.PASSWORD_MAX
    with pytest.raises(ValidationError) as failed:
        _body(password=too_long, connection="")
    errors = failed.value.errors()
    assert any(e.get("input") == too_long for e in errors)
    response = asyncio.run(main._invalid_input(SimpleNamespace(url=SimpleNamespace(path=jl.ROUTE)),
                                               RequestValidationError(errors)))
    body = response.body.decode()
    assert response.status_code == 422
    assert PASSWORD not in body and "xxxxxxxxxx" not in body
    detail = json.loads(body)["detail"]
    assert {"loc", "msg", "type"} == set(detail[0])
    assert any(item["loc"][-1] == "password" for item in detail)


def test_a_422_for_a_non_string_password_does_not_echo_it():
    with pytest.raises(ValidationError) as failed:
        jl.JdbcLoginRequest(connection="orders-sql", secret_key="orders-db", user_name="loader", password=12345678)
    response = asyncio.run(main._invalid_input(SimpleNamespace(url=SimpleNamespace(path=jl.ROUTE)),
                                               RequestValidationError(failed.value.errors())))
    assert "12345678" not in response.body.decode()


def test_other_routes_keep_fastapis_own_422(monkeypatch):
    seen = []

    async def default(request, exc):
        seen.append(request.url.path)
        return SimpleNamespace(status_code=422)

    monkeypatch.setattr(main, "request_validation_exception_handler", default)
    asyncio.run(main._invalid_input(SimpleNamespace(url=SimpleNamespace(path="/api/tables")),
                                    RequestValidationError([])))
    assert seen == ["/api/tables"]
