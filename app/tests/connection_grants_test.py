"""The grants job's USE CONNECTION for the app on the connections it discovers through (connection_grants.py)."""
import pytest

pytest.importorskip("databricks.sdk")
import connection_grants as cg  # noqa: E402


def test_the_connections_are_read_from_a_comma_separated_list():
    assert cg.connection_names(" lfc_sqlserver_test, ,lfc_sqlserver_discover,lfc_sqlserver_test ") == [
        "lfc_sqlserver_test", "lfc_sqlserver_discover"]
    assert cg.connection_names("") == [] and cg.connection_names(None) == []


class _Grants:
    def __init__(self, refuse):
        self.refuse, self.calls = refuse, []

    def update(self, securable_type, full_name, changes):
        self.calls.append((securable_type, full_name, changes[0].principal, [p.value for p in changes[0].add]))
        if full_name in self.refuse:
            raise PermissionError(f"User does not have MANAGE on Connection '{full_name}'.\nmore")


def test_each_connection_is_granted_and_one_the_runner_cant_grant_on_is_reported_without_stopping_the_rest():
    workspace = type("W", (), {"grants": _Grants(refuse={"theirs"})})()
    done = cg.grant_use_connection(workspace, "6f1c-app", ["lfc_sqlserver_test", "theirs", "lfc_sqlserver_discover"])
    assert done == {"lfc_sqlserver_test": None, "theirs": "User does not have MANAGE on Connection 'theirs'.",
                    "lfc_sqlserver_discover": None}
    assert workspace.grants.calls[0] == ("connection", "lfc_sqlserver_test", "6f1c-app", ["USE_CONNECTION"])
    assert len(workspace.grants.calls) == 3
