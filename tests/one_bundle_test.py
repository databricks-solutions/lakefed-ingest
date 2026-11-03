"""One bundle deploys everything: the root databricks.yml includes the operations app's resources, the app is told
the ingestion jobs' ids by reference (no lookups by name), and the deploy itself applies the schema, starts the app
and gives it its access, in that order. Read as text, like the other bundle tests."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = (ROOT / "databricks.yml").read_text()
APP = (ROOT / "app" / "resources" / "lakefed_ingest_app.yml").read_text()
SETUP = (ROOT / "resources" / "lakefed_ingest_setup.yml").read_text()
RESOURCE_FILES = sorted((ROOT / "resources").glob("*.yml")) + sorted((ROOT / "app" / "resources").glob("*.yml"))


def test_the_app_is_part_of_the_root_bundle_and_has_no_bundle_of_its_own():
    assert "include:\n  - resources/*.yml\n  - app/resources/*.yml\n" in BUNDLE
    assert not (ROOT / "app" / "databricks.yml").exists()


def test_the_bundle_asks_for_a_cli_that_runs_jobs_on_every_deploy():
    assert 'databricks_cli_version: ">= 1.13.0"' in BUNDLE


def test_the_app_is_told_each_ingestion_job_by_reference_and_nothing_is_looked_up_by_name():
    referenced = re.findall(r"^            id: \$\{resources\.jobs\.([a-z_]+)\.id\}$", APP, re.M)
    assert referenced == ["lakefed_ingest", "lakefed_ingest_setup", "lakefed_ingest_discover", "lakefed_ingest_check",
                          "lakefed_ingest_cluster_copies", "lakefed_ingest_delete_job"]
    assert set(referenced) <= _job_keys()
    assert "lookup:" not in BUNDLE and "lookup:" not in APP
    assert not re.search(r"^  [a-z_]+_job_id:$", BUNDLE, re.M)


def test_every_variable_a_resource_uses_is_declared_once():
    declared = re.findall(r"^  ([a-z_]+):\n    description:", BUNDLE, re.M)
    assert len(declared) == len(set(declared))
    used = {name for path in RESOURCE_FILES for name in re.findall(r"\$\{var\.([a-z_]+)\}", path.read_text())}
    assert used - set(declared) == set()


def test_the_deploy_starts_the_app():
    assert "      lifecycle:\n        started: true\n" in APP.split("\n  sql_warehouses:\n")[0]


def test_the_deploy_runs_the_setup_job_once_the_database_exists():
    run = _block(SETUP, "    lakefed_ingest_setup_run:\n")
    assert "      job_id: ${resources.jobs.lakefed_ingest_setup.id}\n" in run
    assert "        db_name: ${resources.database_catalogs.lakebase_catalog.database_name}\n" in run
    assert "      queue:\n        enabled: true\n" in run
    assert "          - on_bundle_deploy: true\n" in run


def test_the_app_starts_after_the_setup_run_and_the_grants_job_follows_the_app():
    assert "            value: ${resources.job_runs.lakefed_ingest_setup_run.state.result_state}\n" in APP
    grants_job = _block(APP, "    lakefed_ingest_app_grants:\n")
    assert "              app_name: ${resources.apps.lakefed_ingest_app.name}\n" in grants_job


def test_the_deploy_runs_the_grants_job_every_time():
    run = _block(APP, "    lakefed_ingest_app_grants_run:\n")
    assert "      job_id: ${resources.jobs.lakefed_ingest_app_grants.id}\n" in run
    assert "          - on_bundle_deploy: true\n" in run



def test_the_bundle_has_only_a_development_and_a_production_target_and_neither_names_a_workspace():
    targets = BUNDLE.split("\ntargets:\n", 1)[1]
    assert re.findall(r"^  ([a-z_]+):$", targets, re.M) == ["dev", "prod"]
    assert "host:" not in targets
    assert "  dev:\n    mode: development\n    default: true\n" in targets
    assert "  prod:\n    mode: production\n    workspace:\n" in targets and "root_path: /Workspace/${bundle.name}" in targets


def _job_keys():
    return {key for path in RESOURCE_FILES
            for key in re.findall(r"^    ([a-z_]+):\n      name:", path.read_text().split("\n  jobs:\n", 1)[-1], re.M)}


def _block(text, header):
    """A resource's lines, from its key to the next key at the same depth or the end of the file."""
    start = text.index(header)
    following = re.search(r"^(    [a-z_]+:|  [a-z_]+:)$", text[start + len(header):], re.M)
    return text[start:start + len(header) + following.start()] if following else text[start:]
