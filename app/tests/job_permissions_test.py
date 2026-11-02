"""Run now and Delete follow Databricks' own job permissions (jobs.highest_permission, jobs.action_refusal): the
signed-in user's highest level on the job, directly or through a group, with workspace admins as managers.

Skipped when the app's libraries aren't installed (jobs.py imports the Lakebase client, psycopg and the Databricks
SDK), as the other app tests are, so the repo's base `pytest` runs."""
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("databricks.sdk")
import jobs  # noqa: E402

ACL = [
    {"user_name": "owner@example.com", "all_permissions": [{"permission_level": "IS_OWNER"}]},
    {"user_name": "runner@example.com", "all_permissions": [{"permission_level": "CAN_MANAGE_RUN"}]},
    {"group_name": "ops", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
    {"group_name": "viewers", "all_permissions": [{"permission_level": "CAN_VIEW"}]},
    {"service_principal_name": "app-sp", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
]


def test_the_highest_level_counts_whether_it_is_given_directly_or_through_a_group():
    assert jobs.highest_permission(ACL, "owner@example.com", []) == "IS_OWNER"
    assert jobs.highest_permission(ACL, "Runner@Example.com", ["viewers"]) == "CAN_MANAGE_RUN"
    assert jobs.highest_permission(ACL, "someone@example.com", ["viewers", "ops"]) == "CAN_MANAGE"
    assert jobs.highest_permission(ACL, "someone@example.com", ["viewers"]) == "CAN_VIEW"
    assert jobs.highest_permission(ACL, "nobody@example.com", []) is None
    assert jobs.highest_permission(ACL, "nobody@example.com", ["admins"]) == "CAN_MANAGE"   # a workspace admin


def test_run_now_needs_can_manage_run_and_delete_needs_can_manage_or_ownership():
    assert jobs.action_refusal("run", "CAN_MANAGE_RUN", "the Databricks job x") is None
    assert jobs.action_refusal("run", "CAN_VIEW", "the Databricks job x") == (
        "starting a run needs Can Manage Run on the Databricks job x; you have Can View")
    assert jobs.action_refusal("delete", "CAN_MANAGE_RUN", "the Databricks job x") == (
        "deleting needs Can Manage on the Databricks job x; you have Can Manage Run")
    assert jobs.action_refusal("delete", "IS_OWNER", "the Databricks job x") is None
    assert jobs.action_refusal("delete", None, "the Databricks job x") == (
        "deleting needs Can Manage on the Databricks job x; you have no access to it")
