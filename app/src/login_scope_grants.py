"""WRITE on the dedicated JDBC login scope for the app's service principal, and nothing else. The app saves the logins
typed into it, so WRITE is all it needs: MANAGE would let it change who reads the scope, and READ is not needed to
write. The app bundle creates the scope, so its deployer holds MANAGE; the app's grants job
(notebooks/grant_app_access.ipynb) runs as that deployer and makes this grant. Only the app's own entry is set; the
other principals on the scope are left as they are."""
from databricks.sdk.service.workspace import AclPermission


def grant_write(workspace, scope: str, principal: str) -> bool:
    """Give the principal (the app's service principal application id) WRITE on the scope. Returns True when it made
    the grant, and False when the principal already held WRITE, so a second run changes nothing. An entry at another
    level is replaced by WRITE."""
    for entry in workspace.secrets.list_acls(scope=scope):
        if entry.principal == principal and entry.permission == AclPermission.WRITE:
            return False
    workspace.secrets.put_acl(scope=scope, principal=principal, permission=AclPermission.WRITE)
    return True
