"""The app's CAN_USE on a SQL warehouse of your own (app_warehouse_id), granted by the grants job: an additive update of
the warehouse's access list (update_permissions, the PATCH), never set_permissions, which would replace the list;
nothing at all when the id is blank; and a clear failure, naming who must grant it, when the update is refused. Run
against a fake workspace, and the notebook read as text, like grant_app_access_test.py."""
import json
from pathlib import Path

import pytest
from databricks.sdk.service.sql import WarehousePermissionLevel

import warehouse_grants as wg

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "grant_app_access.ipynb"
APP = "11111111-2222-3333-4444-555555555555"


class _Warehouses:
    def __init__(self, refusal=None):
        self.refusal, self.updates = refusal, []

    def update_permissions(self, warehouse_id, *, access_control_list=None):
        if self.refusal:
            raise self.refusal
        self.updates.append((warehouse_id, access_control_list))

    def set_permissions(self, *args, **kwargs):
        pytest.fail("set_permissions replaces the warehouse's whole access list")


class _Workspace:
    def __init__(self, refusal=None):
        self.warehouses = _Warehouses(refusal)


def test_the_app_is_given_can_use_on_its_warehouse_by_an_additive_update():
    workspace = _Workspace()
    said = wg.grant_app_warehouse(workspace, " wh9 ", APP)
    [(warehouse_id, entries)] = workspace.warehouses.updates
    assert warehouse_id == "wh9"
    assert [(e.service_principal_name, e.permission_level, e.user_name, e.group_name) for e in entries] == [
        (APP, WarehousePermissionLevel.CAN_USE, None, None)]
    assert said == f"granted the app's service principal {APP} CAN_USE on SQL warehouse wh9 (app_warehouse_id)"


def test_a_blank_warehouse_id_grants_nothing():
    for blank in ("", "   ", None):
        workspace = _Workspace()
        said = wg.grant_app_warehouse(workspace, blank, APP)
        assert workspace.warehouses.updates == []
        assert said.startswith("app_warehouse_id is blank: the app uses the bundle's own warehouse")


def test_a_refused_update_fails_and_says_who_must_grant_it():
    workspace = _Workspace(PermissionError("PERMISSION_DENIED: User does not have CAN_MANAGE on warehouse wh9.\nmore"))
    with pytest.raises(wg.NotGranted) as refused:
        wg.grant_app_warehouse(workspace, "wh9", APP)
    message = str(refused.value)
    assert message.startswith(f"can't give the app's service principal {APP} CAN_USE on SQL warehouse wh9 "
                              "(app_warehouse_id): PERMISSION_DENIED: User does not have CAN_MANAGE on warehouse wh9.")
    assert "Whoever runs this job needs CAN_MANAGE on that warehouse" in message
    assert f"its owner or a workspace admin grants CAN_USE on it to {APP}" in message
    assert "more" not in message


def test_the_notebook_reads_app_warehouse_id_and_grants_it_last_through_warehouse_grants():
    cells = [("".join(cell["source"])) for cell in json.loads(NOTEBOOK.read_text())["cells"]]
    assert 'dbutils.widgets.text("app_warehouse_id", "")' in cells[0]
    assert 'app_warehouse_id = dbutils.widgets.get("app_warehouse_id").strip()' in cells[0]
    grant = "print(warehouse_grants.grant_app_warehouse(w, app_warehouse_id, role))"
    assert cells[1].count(grant) == 1
    assert cells[1].index("login_scope_grants.grant_write(") < cells[1].index(grant) < cells[1].index(
        "dbutils.notebook.exit(role)")


def test_nothing_in_the_grants_replaces_an_access_list():
    for source in (NOTEBOOK.read_text(), (Path(wg.__file__)).read_text().split('"""', 2)[2]):
        assert "set_permissions" not in source
    assert "update_permissions(" in Path(wg.__file__).read_text()
