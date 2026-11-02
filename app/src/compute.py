"""What a job runs on, as the job Edit view shows it: every task's compute, read from the job's settings (the Jobs API
``settings`` as a dict), each value with where it comes from.

The ingestion cluster is set in the bundle (resources/lakefed_ingest.yml, from databricks.yml variables), so a value's
"source" names the bundle variable or file that sets it; an app job with its own cluster runs a copy of the ingestion
job whose types and workers that app job sets (cluster_copies)."""
from typing import List, Optional

BUNDLE_FILE = "resources/lakefed_ingest.yml"
OUTSIDE_BUNDLE = "the job's cluster, set outside the bundle variables"
OWN_CLUSTER = "this job's own cluster (Job cluster, above)"


def _field(name: str, value: str, source: str) -> dict:
    return {"name": name, "value": value, "source": source}


def _from_variable(name: str, key: str, value: str, cluster: dict, own_cluster: bool = False) -> dict:
    """A cluster setting the bundle sets from a variable; once the cluster no longer carries the key (an
    autoscale cluster has no num_workers), its value was set some other way. On a job's own copy of the ingestion
    job, the job's own cluster sets it."""
    if own_cluster:
        return _field(name, value, OWN_CLUSTER)
    if key not in cluster:
        return _field(name, value, OUTSIDE_BUNDLE)
    return _field(name, value, f"bundle variable {key} (databricks.yml)")


def _workers(cluster: dict) -> str:
    autoscale = cluster.get("autoscale")
    if autoscale:
        return f"autoscale {autoscale.get('min_workers')} to {autoscale.get('max_workers')}"
    workers = int(cluster.get("num_workers") or 0)
    return f"{workers} (single node)" if workers == 0 else str(workers)


def _settings_text(pairs: Optional[dict]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((pairs or {}).items())) or "none"


def _cluster_fields(cluster: dict, own_cluster: bool = False) -> List[dict]:
    workers_node = cluster.get("node_type_id") or "the workspace default"
    driver = cluster.get("driver_node_type_id") or "same as the workers"
    return [
        _field("Runtime", cluster.get("spark_version") or "the workspace default", BUNDLE_FILE),
        _from_variable("Driver node type", "driver_node_type_id", driver, cluster, own_cluster),
        _from_variable("Worker node type", "node_type_id", workers_node, cluster, own_cluster),
        _from_variable("Workers", "num_workers", _workers(cluster), cluster, own_cluster),
        _field("Access mode", cluster.get("data_security_mode") or "the workspace default", BUNDLE_FILE),
        _field("Spark settings", _settings_text(cluster.get("spark_conf")), BUNDLE_FILE),
        _field("Tags", _settings_text(cluster.get("custom_tags")), BUNDLE_FILE),
    ]


def describe(settings: dict, own_cluster: bool = False) -> List[dict]:
    """One entry per task of a job: its kind of compute (job cluster, serverless or an existing cluster) and the
    fields to show, each {name, value, source}. ``own_cluster``: the job is an app job's copy of the ingestion job,
    whose cluster types and workers that app job sets."""
    clusters = {c["job_cluster_key"]: c.get("new_cluster") or {} for c in settings.get("job_clusters") or []}
    environments = {e["environment_key"]: e.get("spec") or {} for e in settings.get("environments") or []}
    out = []
    for task in settings.get("tasks") or []:
        entry = {"task": task.get("task_key"), "cluster": None}
        if task.get("job_cluster_key") in clusters:
            entry.update(kind="job cluster", cluster=task["job_cluster_key"],
                         fields=_cluster_fields(clusters[task["job_cluster_key"]], own_cluster))
        elif task.get("new_cluster"):
            entry.update(kind="job cluster", fields=_cluster_fields(task["new_cluster"], own_cluster))
        elif task.get("existing_cluster_id"):
            entry.update(kind="existing cluster",
                         fields=[_field("Cluster id", task["existing_cluster_id"], BUNDLE_FILE)])
        else:
            key = task.get("environment_key")
            version = (environments.get(key) or {}).get("environment_version")
            label = f"{key}, version {version}" if key and version else (key or "the default environment")
            entry.update(kind="serverless", fields=[_field("Environment", label, "the job's environments")])
        out.append(entry)
    return out
