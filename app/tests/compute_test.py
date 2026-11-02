"""What a job runs on, as the job Edit view shows it (compute.describe): read from the job's settings, each value with
where it comes from, and serverless tasks named as serverless."""
import compute

INGEST = {
    "name": "lakefed_ingest",
    "job_clusters": [{"job_cluster_key": "sweep_cluster", "new_cluster": {
        "spark_version": "18.x-scala2.13", "data_security_mode": "SINGLE_USER",
        "node_type_id": "Standard_D8ds_v5", "driver_node_type_id": "Standard_D16ds_v5", "num_workers": 1,
        "spark_conf": {"spark.scheduler.mode": "FAIR"}, "custom_tags": {"project": "lakefed_ingest"}}}],
    "tasks": [{"task_key": "ingest", "job_cluster_key": "sweep_cluster"}],
}
CHECK = {"name": "lakefed_ingest_check", "tasks": [{"task_key": "check", "environment_key": "lakebase_client"}],
         "environments": [{"environment_key": "lakebase_client", "spec": {"environment_version": "4"}}]}


def _fields(entry):
    return {f["name"]: f for f in entry["fields"]}


def test_a_job_cluster_lists_its_settings_each_with_its_source():
    [entry] = compute.describe(INGEST)
    assert (entry["task"], entry["kind"], entry["cluster"]) == ("ingest", "job cluster", "sweep_cluster")
    fields = _fields(entry)
    assert fields["Driver node type"]["value"] == "Standard_D16ds_v5"
    assert fields["Driver node type"]["source"] == "bundle variable driver_node_type_id (databricks.yml)"
    assert "tested" not in fields["Driver node type"]
    assert fields["Workers"]["value"] == "1"
    assert fields["Workers"]["source"] == "bundle variable num_workers (databricks.yml)"
    assert fields["Runtime"]["source"] == "resources/lakefed_ingest.yml"
    assert fields["Spark settings"]["value"] == "spark.scheduler.mode=FAIR"


def test_autoscale_shows_its_range_instead_of_a_worker_count():
    cluster = dict(INGEST["job_clusters"][0]["new_cluster"], autoscale={"min_workers": 1, "max_workers": 4})
    del cluster["num_workers"]
    settings = dict(INGEST, job_clusters=[{"job_cluster_key": "sweep_cluster", "new_cluster": cluster}])
    fields = _fields(compute.describe(settings)[0])
    assert fields["Workers"]["value"] == "autoscale 1 to 4"
    assert fields["Workers"]["source"] == "the job's cluster, set outside the bundle variables"


def test_a_serverless_task_says_serverless_and_names_its_environment():
    [entry] = compute.describe(CHECK)
    assert (entry["task"], entry["kind"]) == ("check", "serverless")
    assert _fields(entry)["Environment"]["value"] == "lakebase_client, version 4"


def test_a_task_on_an_existing_cluster_names_that_cluster():
    settings = {"tasks": [{"task_key": "discover", "existing_cluster_id": "0101-abc"}]}
    [entry] = compute.describe(settings)
    assert (entry["kind"], _fields(entry)["Cluster id"]["value"]) == ("existing cluster", "0101-abc")


def test_an_empty_setting_shows_as_the_cluster_default_rather_than_blank():
    cluster = {"spark_version": "18.x-scala2.13", "node_type_id": "Standard_D8ds_v5", "num_workers": 0}
    settings = {"job_clusters": [{"job_cluster_key": "c", "new_cluster": cluster}],
                "tasks": [{"task_key": "t", "job_cluster_key": "c"}]}
    fields = _fields(compute.describe(settings)[0])
    assert fields["Driver node type"]["value"] == "same as the workers"
    assert fields["Workers"]["value"] == "0 (single node)"
    assert fields["Spark settings"]["value"] == "none"


def test_on_a_jobs_own_copy_the_types_and_workers_are_that_jobs_own():
    fields = _fields(compute.describe(INGEST, own_cluster=True)[0])
    assert fields["Driver node type"]["source"] == "this job's own cluster (Job cluster, above)"
    assert fields["Runtime"]["source"] == "resources/lakefed_ingest.yml"
