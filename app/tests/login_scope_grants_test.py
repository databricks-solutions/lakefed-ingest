"""The app's WRITE on the dedicated login scope, granted by the grants job: exactly WRITE, to exactly the app's service
principal, once. Run against a fake workspace that keeps the scope's access list."""
from databricks.sdk.service.workspace import AclItem, AclPermission

import login_scope_grants as lsg

SCOPE = "lakefed_ingest_jdbc_logins"
APP = "11111111-2222-3333-4444-555555555555"
DEPLOYER = "deployer@example.com"
OTHER = "someone-else@example.com"


class _Secrets:
    def __init__(self, entries):
        self.entries = dict(entries)
        self.puts = []

    def list_acls(self, scope):
        assert scope == SCOPE
        return [AclItem(principal=principal, permission=permission) for principal, permission in self.entries.items()]

    def put_acl(self, scope, principal, permission):
        assert scope == SCOPE
        self.puts.append((principal, permission))
        self.entries[principal] = permission


class _Workspace:
    def __init__(self, entries):
        self.secrets = _Secrets(entries)


def test_the_app_is_granted_write_on_the_scope_when_it_has_no_entry():
    workspace = _Workspace([(DEPLOYER, AclPermission.MANAGE)])
    assert lsg.grant_write(workspace, SCOPE, APP) is True
    assert workspace.secrets.puts == [(APP, AclPermission.WRITE)]
    assert workspace.secrets.entries == {DEPLOYER: AclPermission.MANAGE, APP: AclPermission.WRITE}


def test_a_run_again_grants_nothing_when_the_app_already_holds_write():
    workspace = _Workspace([(DEPLOYER, AclPermission.MANAGE), (APP, AclPermission.WRITE)])
    assert lsg.grant_write(workspace, SCOPE, APP) is False
    assert workspace.secrets.puts == []


def test_two_runs_in_a_row_grant_once():
    workspace = _Workspace([(DEPLOYER, AclPermission.MANAGE)])
    lsg.grant_write(workspace, SCOPE, APP)
    assert lsg.grant_write(workspace, SCOPE, APP) is False
    assert workspace.secrets.puts == [(APP, AclPermission.WRITE)]


def test_an_entry_at_another_level_is_corrected_to_write_and_never_manage_or_read():
    workspace = _Workspace([(DEPLOYER, AclPermission.MANAGE), (APP, AclPermission.READ)])
    assert lsg.grant_write(workspace, SCOPE, APP) is True
    assert workspace.secrets.puts == [(APP, AclPermission.WRITE)]
    assert workspace.secrets.entries[APP] == AclPermission.WRITE


def test_no_other_principal_is_changed():
    workspace = _Workspace([(DEPLOYER, AclPermission.MANAGE), (OTHER, AclPermission.READ)])
    lsg.grant_write(workspace, SCOPE, APP)
    assert all(principal == APP for principal, _ in workspace.secrets.puts)
    assert workspace.secrets.entries[OTHER] == AclPermission.READ
    assert workspace.secrets.entries[DEPLOYER] == AclPermission.MANAGE
