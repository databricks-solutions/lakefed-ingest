"""The ingestion job's per-app-job copies (cluster_copies): what a copy is, and when one is made, kept or deleted."""
import json
from pathlib import Path

import cluster_copies as cc

NOTEBOOK = Path(__file__).parent.parent / "src" / "lakefed_ingest" / "cluster_copies.ipynb"

BUNDLE = {
    "name": "[dev someone] lakefed_ingest", "max_concurrent_runs": 100,
    "schedule": {"quartz_cron_expression": "0 0/15 * * * ?", "timezone_id": "UTC", "pause_status": "PAUSED"},
    "deployment": {"kind": "BUNDLE", "metadata_file_path": "/x/metadata.json"}, "edit_mode": "UI_LOCKED",
    "job_clusters": [{"job_cluster_key": "sweep_cluster", "new_cluster": {
        "spark_version": "18.x-scala2.13", "node_type_id": "Standard_D8ds_v5", "driver_node_type_id": "Standard_D16ds_v5",
        "num_workers": 1, "spark_conf": {"spark.scheduler.mode": "FAIR"}}}],
    "tasks": [{"task_key": "ingest", "job_cluster_key": "sweep_cluster", "notebook_task": {"notebook_path": "/x/ingest"}}],
    "parameters": [{"name": "task_collection", "default": ""}], "tags": {"project": "lakefed_ingest"},
}
BIG = {"driver_node_type_id": "Standard_D32ds_v5", "node_type_id": "Standard_D16ds_v5", "num_workers": 4}


def schedule(sid, name="Hourly", cluster=None, deleted=False):
    return {"id": sid, "name": name, "options": {"cluster": cluster} if cluster else {},
            "deleted_at": "2026-10-28" if deleted else None}


def found_copy(job_id, schedule_id="7", owner="inst/db"):
    """A Jobs API list entry, as the lookup by copy_name returns it."""
    return {"job_id": job_id, "settings": {"name": cc.copy_name("Hourly"),
                                           "tags": {cc.TAG_SCHEDULE: schedule_id, cc.TAG_OWNER: owner}}}


def test_a_copy_of_this_schedule_and_owner_is_adopted():
    assert cc.adoptable([found_copy(902)], 7, "inst/db") == 902


def test_a_copy_of_another_schedule_with_the_same_name_is_not_adopted():
    assert cc.adoptable([found_copy(902, schedule_id="8")], 7, "inst/db") is None


def test_a_copy_of_another_deployment_is_not_adopted():
    assert cc.adoptable([found_copy(902, owner="other/db")], 7, "inst/db") is None


def test_with_no_copy_found_nothing_is_adopted_and_a_job_without_our_tags_is_left_alone():
    untagged = {"job_id": 905, "settings": {"name": cc.copy_name("Hourly")}}
    assert cc.adoptable([], 7, "inst/db") is None
    assert cc.adoptable([untagged], 7, "inst/db") is None
    assert cc.surplus([untagged], 7, "inst/db") == []


def test_two_copies_adopt_the_lowest_id_and_the_other_is_listed_for_removal_by_hand():
    found = [found_copy(904), found_copy(902), found_copy(903, schedule_id="8")]
    assert cc.adoptable(found, 7, "inst/db") == 902
    assert cc.surplus(found, 7, "inst/db") == [
        {"job_id": 904, "name": cc.copy_name("Hourly"), "tags": {cc.TAG_SCHEDULE: "7", cc.TAG_OWNER: "inst/db"}}]


def test_the_plan_lists_an_extra_copy_and_never_deletes_it():
    extra = cc.surplus([found_copy(902), found_copy(904)], 7, "inst/db")
    actions = cc.plan([schedule(7, cluster=BIG)], {"7": 902}, BUNDLE, "inst/db", extras={"7": extra})
    assert [a["action"] for a in actions] == ["update", "extra"]
    assert actions[1] == {"action": "extra", "schedule_id": 7, "job_id": 904, "name": cc.copy_name("Hourly"),
                          "tags": {cc.TAG_SCHEDULE: "7", cc.TAG_OWNER: "inst/db"}}


def test_the_notebook_finds_a_copy_by_name_and_tags_before_it_creates_one():
    code = "\n".join(c["source"] for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
                     if c["cell_type"] == "code")
    lookup = code.index("w.jobs.list(name=cc.copy_name(")
    assert lookup < code.index("cc.plan(") < code.index('"/api/2.2/jobs/create"')
    assert code.index("cc.adoptable(") < code.index("cc.plan(")


def test_the_notebook_only_lists_extra_copies_and_deletes_only_a_planned_delete():
    code = "\n".join(c["source"] for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
                     if c["cell_type"] == "code")
    assert code.count("w.jobs.delete(") == 1 and 'w.jobs.delete(action["job_id"])' in code
    assert '"extra"' in code and "by hand" in code


def test_a_copy_is_the_bundle_job_with_only_its_cluster_changed_and_no_trigger_of_its_own():
    s = cc.copy_settings(BUNDLE, schedule(7, cluster=BIG), BIG, "inst/db")
    cluster = s["job_clusters"][0]["new_cluster"]
    assert (cluster["driver_node_type_id"], cluster["node_type_id"], cluster["num_workers"]) == (
        "Standard_D32ds_v5", "Standard_D16ds_v5", 4)
    assert cluster["spark_conf"] == {"spark.scheduler.mode": "FAIR"} and s["tasks"] == BUNDLE["tasks"]
    assert s["parameters"] == BUNDLE["parameters"] and s["max_concurrent_runs"] == 100
    assert not {"schedule", "deployment", "edit_mode"} & set(s)
    assert s["name"] == "lakefed_ingest: Hourly"
    assert s["tags"] == {"project": "lakefed_ingest", cc.TAG_SCHEDULE: "7", cc.TAG_OWNER: "inst/db"}
    assert BUNDLE["job_clusters"][0]["new_cluster"]["num_workers"] == 1      # the bundle's settings are untouched


def test_a_worker_count_replaces_an_autoscale_range():
    bundle = {**BUNDLE, "job_clusters": [{"job_cluster_key": "c", "new_cluster": {"autoscale": {"min_workers": 1, "max_workers": 8}}}]}
    cluster = cc.copy_settings(bundle, schedule(1), {"num_workers": "2"}, "o")["job_clusters"][0]["new_cluster"]
    assert cluster == {"num_workers": 2}


def test_jobs_with_their_own_cluster_get_a_copy_and_jobs_back_at_the_defaults_lose_theirs():
    schedules = [schedule(1, "New", BIG), schedule(2, "Kept", BIG), schedule(3, "Back to defaults"),
                 schedule(4, "Deleted", BIG, deleted=True)]
    actions = cc.plan(schedules, {"2": 902, "3": 903, "4": 904, "99": 999}, BUNDLE, "o")
    assert [(a["action"], a["schedule_id"], a.get("job_id")) for a in actions] == [
        ("create", 1, None), ("update", 2, 902), ("delete", 3, 903), ("delete", 4, 904)]
    # A copy of a schedule this run wasn't asked about (99) is left alone.


def test_blank_cluster_values_mean_the_bundle_jobs_own():
    assert cc.cluster_of(schedule(1, cluster={"driver_node_type_id": "", "num_workers": 2})) == {"num_workers": 2}
    assert cc.cluster_of(schedule(1)) is None


def test_a_service_principal_deployer_is_named_by_its_application_id_in_the_copy_permissions():
    app_id = "6e1c9b4a-1f2e-4d3c-9a8b-7c6d5e4f3a2b"
    acl = [{"service_principal_name": app_id, "all_permissions": [{"permission_level": "IS_OWNER"}]},
           {"group_name": "operators", "all_permissions": [{"permission_level": "CAN_MANAGE_RUN"}]}]
    assert cc.copy_permissions(acl, "app-sp", app_id) == [
        {"service_principal_name": app_id, "permission_level": "IS_OWNER"},
        {"group_name": "operators", "permission_level": "CAN_MANAGE_RUN"},
        {"service_principal_name": "app-sp", "permission_level": "CAN_MANAGE"}]


def test_the_copy_is_recorded_before_its_permissions_are_set_so_a_failure_there_is_found_next_run():
    cells = [c["source"] for c in json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"] if c["cell_type"] == "code"]
    code = next(cell for cell in cells if "copy_permissions" in cell)
    assert code.rindex("record(sid, job_id)") < code.index('"PUT"')


def test_a_copy_is_shared_as_the_ingestion_job_is_with_the_app_managing_it():
    acl = [{"user_name": "deployer@x.com", "all_permissions": [{"permission_level": "IS_OWNER"}]},
           {"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE", "inherited": True}]},
           {"group_name": "operators", "all_permissions": [{"permission_level": "CAN_MANAGE_RUN"}]},
           {"service_principal_name": "app-sp", "all_permissions": [{"permission_level": "CAN_MANAGE"}]}]
    assert cc.copy_permissions(acl, "app-sp", "deployer@x.com") == [
        {"user_name": "deployer@x.com", "permission_level": "IS_OWNER"},
        {"group_name": "operators", "permission_level": "CAN_MANAGE_RUN"},
        {"service_principal_name": "app-sp", "permission_level": "CAN_MANAGE"}]
