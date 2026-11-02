"""USE CONNECTION for the app's service principal on the Unity Catalog connections it discovers through: the app
lists connections as its own identity, so it sees only those it holds USE CONNECTION on. The connections are not
the bundle's, so the app's grants job (notebooks/grant_app_access.ipynb) grants it as whoever runs that job, on
each connection named in the app bundle's source_connections variable."""
from typing import Dict, List, Optional


def connection_names(text: str) -> List[str]:
    """The connections named in source_connections: comma-separated, blanks and repeats dropped, in order."""
    seen: Dict[str, None] = {}
    for name in (text or "").split(","):
        if name.strip():
            seen.setdefault(name.strip(), None)
    return list(seen)


def grant_use_connection(workspace, principal: str, names: List[str]) -> Dict[str, Optional[str]]:
    """Grant USE CONNECTION on each connection to the principal (the app's service principal application id).
    Returns {connection: None when granted, else why not}: a connection the runner can't grant on is reported,
    and the others are still granted."""
    from databricks.sdk.service.catalog import PermissionsChange, Privilege
    out: Dict[str, Optional[str]] = {}
    for name in names:
        try:
            workspace.grants.update(securable_type="connection", full_name=name,
                                    changes=[PermissionsChange(principal=principal, add=[Privilege.USE_CONNECTION])])
            out[name] = None
        except Exception as e:      # not found, or the runner may not grant on it: say so and go on
            out[name] = str(e).strip().split("\n")[0][:300]
    return out
