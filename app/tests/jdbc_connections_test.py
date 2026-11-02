"""JDBC connections entered by hand: the validation of a connection's names and where its source is, the job parameters
its jobs run with, and what the API shows of a stored row (never a secret). Plain functions, no database."""
from datetime import datetime, timezone

import pytest

import jdbc_connections as jc

GOOD = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example", "src_port": 1433,
        "secret_scope": "lakefed", "secret_key": "orders-login", "trust_server_certificate": False}


def _errors(**changes) -> list:
    return jc.validate({**GOOD, **changes})[1]


def test_a_good_connection_has_no_errors_and_keeps_its_names():
    clean, errors = jc.validate(GOOD)
    assert errors == []
    assert clean == {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example",
                     "src_port": 1433, "secret_scope": "lakefed", "secret_key": "orders-login",
                     "trust_server_certificate": False}


def test_a_blank_port_reads_the_source_types_default_port():
    assert jc.validate({**GOOD, "src_port": None, "src_type": "postgresql"})[0]["src_port"] == 5432
    assert jc.validate({**GOOD, "src_port": "", "src_type": "mysql"})[0]["src_port"] == 3306


@pytest.mark.parametrize("port", [0, 65536, -1, "1433", True, 14.5])
def test_a_port_out_of_range_or_not_a_whole_number_is_refused(port):
    assert any("port" in e for e in _errors(src_port=port))


@pytest.mark.parametrize("host", ["", "https://orders.example", "orders.example/db", "orders.example:1433",
                                  "bad host", "-orders.example", "fe80::1"])
def test_a_host_must_be_a_plain_host_name_or_address(host):
    assert any("host" in e for e in _errors(src_host=host))


def test_a_source_type_outside_the_adapters_is_refused():
    assert any("source type" in e for e in _errors(src_type="db2"))
    assert jc.validate({**GOOD, "src_type": "Oracle"})[1] == []


@pytest.mark.parametrize("scope", ["", "lake fed", "lake/fed", "x" * 129])
def test_a_scope_name_must_be_a_valid_secret_scope_name(scope):
    assert any("scope" in e for e in _errors(secret_scope=scope))


@pytest.mark.parametrize("key", ["", "orders login", "orders_login", "orders.login", "orders/login"])
def test_a_key_must_follow_the_key_vault_rule(key):
    assert any("key" in e for e in _errors(secret_key=key))


def test_a_key_may_hold_the_database_placeholder_once_the_rest_is_key_vault_safe():
    assert _errors(secret_key="creds-{database}") == []
    assert _errors(secret_key="{database}") == []
    assert any("key" in e for e in _errors(secret_key="creds-{schema}"))


def test_a_name_must_be_short_and_plain():
    assert _errors(name="") and _errors(name="x" * 65) and _errors(name="-orders")
    assert _errors(name="Orders (prod)")


def test_trust_defaults_to_verify_and_true_means_trust():
    assert jc.validate({k: v for k, v in GOOD.items() if k != "trust_server_certificate"})[0][
        "trust_server_certificate"] is False
    assert jc.validate({**GOOD, "trust_server_certificate": True})[0]["trust_server_certificate"] is True


def test_the_job_parameters_are_the_scope_and_the_trust_as_the_engine_reads_them():
    assert jc.job_parameters({"secret_scope": "lakefed", "trust_server_certificate": "false"}) == {
        "secret_scope": "lakefed", "trust_server_certificate": "false"}
    assert jc.job_parameters({"secret_scope": "lakefed", "trust_server_certificate": "true"}) == {
        "secret_scope": "lakefed", "trust_server_certificate": "true"}
    assert jc.job_parameters({"secret_scope": "lakefed", "trust_server_certificate": None})[
        "trust_server_certificate"] == "false"


def test_the_insert_params_store_trust_as_text_and_leave_no_creator_blank():
    clean, _ = jc.validate({**GOOD, "trust_server_certificate": True})
    params = jc.insert_params(clean, "")
    assert params == ("orders jdbc", "sqlserver", "orders.internal.example", 1433, "lakefed", "orders-login", "true",
                      None)


def test_the_public_row_names_the_trust_in_words_and_never_has_a_secret_in_it():
    row = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "h", "src_port": 1433, "secret_scope": "lakefed",
           "secret_key": "orders-login", "trust_server_certificate": "false", "created_by": "ann",
           "created_at": datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)}
    shown = jc.public(row)
    assert shown["trust"] == "verify" and shown["created_at"] == "2026-10-30T15:00:00+00:00"
    assert set(shown) == {"name", "src_type", "src_host", "src_port", "secret_scope", "secret_key", "trust",
                          "created_by", "created_at"}
    assert jc.public({**row, "trust_server_certificate": "true"})["trust"] == "trust"



def test_the_public_row_shows_the_creation_time_whether_it_comes_back_as_text_or_a_datetime():
    row = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "h", "src_port": 1433, "secret_scope": "lakefed",
           "secret_key": "orders-login", "trust_server_certificate": "false", "created_by": "ann"}
    as_datetime = jc.public({**row, "created_at": datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)})
    as_text = jc.public({**row, "created_at": "2026-10-30T15:00:00+00:00"})
    assert as_datetime["created_at"] == as_text["created_at"] == "2026-10-30T15:00:00+00:00"
    assert jc.public({**row, "created_at": None})["created_at"] is None

def test_the_store_statements_name_only_the_connection_columns():
    assert not any(word in jc.insert_sql() + jc.list_sql() + jc.get_sql() for word in ("password", "secret_value"))
    assert jc.insert_sql().endswith("on conflict (name) do nothing returning name")
    assert jc.get_sql().endswith("where name = %s")
