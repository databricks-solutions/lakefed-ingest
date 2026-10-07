import os

import pytest

import validation_harness as vh  # same dir; pytest prepends the tests dir to sys.path


def test_reconcile_ok_when_identical():
    src = {1: (1, "a"), 2: (2, "b")}
    assert vh.reconcile(src, dict(src))["ok"] is True


def test_reconcile_flags_missed_insert_delete_and_update():
    source = {1: (1, "x"), 2: (2, "y")}      # row 2 should exist
    bronze = {1: (1, "OLD"), 3: (3, "z")}     # 2 missing, 3 extra (missed delete), 1 stale
    rep = vh.reconcile(source, bronze)
    assert rep["ok"] is False
    assert rep["missing"] == [2]      # missed insert
    assert rep["extra"] == [3]        # missed delete
    assert rep["mismatched"] == [1]   # missed update


def test_extract_creds_flat_and_nested():
    assert vh.extract_creds('{"user": "u", "password": "p"}') == ("u", "p")
    assert vh.extract_creds({"dba": {"login": "u2", "pwd": "p2"}}) == ("u2", "p2")


def test_extract_creds_raises_when_missing():
    with pytest.raises(ValueError):
        vh.extract_creds('{"host": "h"}')


@pytest.mark.skipif(
    not os.environ.get("MT_TEST_SQLSERVER_USER"),
    reason="set MT_TEST_SQLSERVER_{USER,PASSWORD} (+ optional HOST/DB) to hit the Azure SQL test DB",
)
def test_sqlserver_source_roundtrip():
    src = vh.SqlServerTestSource.from_env()
    src.create_ct_table("dbo", "mt_ct_test")
    assert isinstance(src.snapshot("dbo", "mt_ct_test"), dict)
