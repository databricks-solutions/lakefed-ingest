"""CAN_USE for the app's service principal on a SQL warehouse of the customer's own, named in the app bundle's
app_warehouse_id variable, which the app then runs its queries on (warehouse.py). The bundle's own warehouse needs no
grant here: the app holds CAN_USE on it as an app resource. A warehouse the bundle doesn't make can't be an app
resource that a variable switches, so the app's grants job (notebooks/grant_app_access.ipynb) grants it instead, as
whoever runs that job.

The grant is an additive update of the warehouse's access list (the Permissions API's PATCH, update_permissions): the
other entries on the warehouse stay as they are. Never set_permissions, which replaces the whole list."""


class NotGranted(RuntimeError):
    """Whoever runs the grants job may not manage the warehouse, or it isn't there."""


def grant_app_warehouse(workspace, warehouse_id: str, principal: str) -> str:
    """Give the principal (the app's service principal application id) CAN_USE on the warehouse, and say what was done.
    A blank id grants nothing. Raises NotGranted, naming who must grant it, when the update is refused."""
    from databricks.sdk.service.sql import WarehouseAccessControlRequest, WarehousePermissionLevel
    warehouse_id = (warehouse_id or "").strip()
    if not warehouse_id:
        return ("app_warehouse_id is blank: the app uses the bundle's own warehouse, which it holds CAN_USE on as an "
                "app resource; nothing to grant")
    try:
        workspace.warehouses.update_permissions(warehouse_id, access_control_list=[WarehouseAccessControlRequest(
            service_principal_name=principal, permission_level=WarehousePermissionLevel.CAN_USE)])
    except Exception as e:
        reason = str(e).strip().split("\n")[0][:300]
        raise NotGranted(
            f"can't give the app's service principal {principal} CAN_USE on SQL warehouse {warehouse_id} "
            f"(app_warehouse_id): {reason}. Whoever runs this job needs CAN_MANAGE on that warehouse. Otherwise its "
            f"owner or a workspace admin grants CAN_USE on it to {principal} (the warehouse's Permissions, under SQL "
            f"Warehouses), or gives the deployer CAN_MANAGE and runs this job again. The grants above are made.") from e
    return f"granted the app's service principal {principal} CAN_USE on SQL warehouse {warehouse_id} (app_warehouse_id)"
