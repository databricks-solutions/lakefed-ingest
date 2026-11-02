"""The engine's guard on logins typed into the operations app: each goes only to the host of the JDBC connection it was
typed for, whatever a control row or a discovery spec names. Plain functions: no Spark, no secret read, no database
(the query's round trip is in state_store_pg_test.py)."""
import pytest

import ct_engine

SCOPE = "lakefed_ingest_jdbc_logins"
TYPED = (SCOPE, {"creds-sales": {"connection": "orders", "src_host": "orders.example", "src_port": 1433,
                                 "src_type": "sqlserver"},
                 "creds-gone": {"connection": "gone", "src_host": None, "src_port": None, "src_type": None}})


@pytest.fixture
def guarded(monkeypatch):
    """The guard set as a run sets it, and a secret read that records which keys were read."""
    read = []
    monkeypatch.setattr(ct_engine, "_typed_logins", None)
    monkeypatch.setattr(ct_engine, "_config_cache", {})
    monkeypatch.setattr(ct_engine, "_creds", lambda scope, key: read.append((scope, key)) or ("u", "p", None))
    monkeypatch.setenv("LAKEFED_SQL_SECRET_SCOPE", SCOPE)
    ct_engine.guard_typed_logins(*TYPED)
    return read


def _cfg(**fields):
    return {"id": 7, "src_type": "sqlserver", "src_host": "orders.example", "src_port": 1433,
            "secret_key": "creds-sales", **fields}


def test_a_typed_login_goes_to_its_own_connections_host(guarded):
    assert ct_engine._jdbc_config(_cfg(src_host="Orders.Example", src_port=None))["host"] == "Orders.Example"
    assert guarded == [(SCOPE, "creds-sales")]


@pytest.mark.parametrize("changed", [{"src_host": "attacker.example"}, {"src_port": 1444},
                                     {"src_type": "postgresql", "src_port": 1433}])
def test_a_typed_login_is_not_read_for_another_host_port_or_type(guarded, changed):
    with pytest.raises(ValueError, match="typed for the JDBC connection orders"):
        ct_engine._jdbc_config(_cfg(**changed))
    assert guarded == []


def test_a_typed_login_whose_connection_is_not_stored_goes_nowhere(guarded):
    with pytest.raises(ValueError, match="not stored"):
        ct_engine._jdbc_config(_cfg(secret_key="creds-gone"))


def test_a_typed_login_with_no_host_on_the_row_is_refused(guarded):
    with pytest.raises(ValueError, match="names none"):
        ct_engine._jdbc_config(_cfg(src_host=None))


def test_a_key_no_one_typed_and_another_scope_read_as_before(guarded, monkeypatch):
    assert ct_engine._jdbc_config(_cfg(secret_key="admin-written", src_host="anywhere.example"))["port"] == 1433
    monkeypatch.setenv("LAKEFED_SQL_SECRET_SCOPE", "lakefed_ingest")
    assert ct_engine._jdbc_config(_cfg(src_host="anywhere.example"))["host"] == "anywhere.example"


def test_with_no_guard_set_every_read_is_as_before(monkeypatch):
    monkeypatch.setattr(ct_engine, "_typed_logins", None)
    monkeypatch.setattr(ct_engine, "_config_cache", {})
    monkeypatch.setattr(ct_engine, "_creds", lambda scope, key: ("u", "p", None))
    monkeypatch.setenv("LAKEFED_SQL_SECRET_SCOPE", SCOPE)
    assert ct_engine._jdbc_config(_cfg(src_host="attacker.example"))["host"] == "attacker.example"


def test_the_refusal_reads_the_default_port_of_the_logins_own_type():
    typed = (SCOPE, {"k": {"connection": "pg", "src_host": "pg.example", "src_port": None, "src_type": "postgresql"}})
    assert ct_engine.typed_login_refusal(SCOPE, "k", "pg.example", 5432, "postgresql", typed) is None
    assert ct_engine.typed_login_refusal(SCOPE, "k", "pg.example", 1433, "postgresql", typed)
    assert ct_engine.typed_login_refusal("other", "k", "attacker.example", 1, "sqlserver", typed) is None
