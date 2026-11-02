"""The ingestion job's parameters line up: every widget the notebook reads is passed by the job task
and declared as a job parameter, and each tuning parameter defaults to the bundle variable of the same
name (the collection, the work list and the Lakebase names have their own defaults)."""
import json
import re
from pathlib import Path

import discovery
import table_retry

ROOT = Path(__file__).resolve().parents[1]
OWN_DEFAULTS = {"task_collection", "work_id", "db_instance", "db_name"}


def _code(notebook_path="src/lakefed_ingest/ingest.ipynb"):
    notebook = json.loads((ROOT / notebook_path).read_text())
    return "\n".join(c["source"] if isinstance(c["source"], str) else "".join(c["source"])
                     for c in notebook["cells"] if c["cell_type"] == "code")


def _widgets(notebook_path="src/lakefed_ingest/ingest.ipynb"):
    return set(re.findall(r'widget\("([a-z_]+)"', _code(notebook_path)))


def test_every_notebook_widget_is_a_job_parameter_with_a_bundle_variable():
    job = (ROOT / "resources/lakefed_ingest.yml").read_text()
    bundle = (ROOT / "databricks.yml").read_text()
    passed = set(re.findall(r'^\s+([a-z_]+): "\{\{job\.parameters\.\1\}\}"', job, re.M))
    declared = set(re.findall(r"^\s+- name: ([a-z_]+)$", job, re.M))
    defaults = dict(re.findall(r"^\s+- name: ([a-z_]+)\n\s+default: \$\{var\.([a-z_]+)\}", job, re.M))
    variables = set(re.findall(r"^  ([a-z_]+):\n    description:", bundle, re.M))
    widgets = _widgets()
    assert {"table_retries", "table_retry_budget"} <= widgets
    assert widgets - passed == set()
    assert widgets - declared == set()
    tuning = widgets - OWN_DEFAULTS
    assert {w for w in tuning if defaults.get(w) != w} == set()
    assert tuning - variables == set()


def test_table_retries_default_to_two():
    bundle = (ROOT / "databricks.yml").read_text()
    assert re.search(r'^  table_retries:\n    description: .*\n    default: "2"$', bundle, re.M)


def test_table_retry_budget_defaults_to_zero_in_the_bundle_and_the_engine():
    bundle = (ROOT / "databricks.yml").read_text()
    assert re.search(r'^  table_retry_budget:\n    description: .*\n    default: "0"$', bundle, re.M)
    assert table_retry.DEFAULT_BUDGET == 0
    assert 'widget("table_retry_budget", str(table_retry.DEFAULT_BUDGET))' in _code()


def test_every_discover_widget_is_a_job_parameter_and_the_workers_and_cap_take_bundle_variables():
    job = (ROOT / "resources/lakefed_ingest_discover.yml").read_text()
    bundle = (ROOT / "databricks.yml").read_text()
    passed = set(re.findall(r'^\s+([a-z_]+): "\{\{job\.parameters\.\1\}\}"', job, re.M))
    declared = set(re.findall(r"^\s+- name: ([a-z_]+)$", job, re.M))
    variables = set(re.findall(r"^  ([a-z_]+):\n    description:", bundle, re.M))
    widgets = _widgets("src/lakefed_ingest/discover_tables.ipynb")
    assert {"discover_workers", "max_queries_per_source"} <= widgets
    assert widgets - passed == set()
    assert widgets - declared == set()
    for name in ("discover_workers", "max_queries_per_source"):
        assert re.search(rf"^\s+- name: {name}\n\s+default: \${{var\.{name}}}$", job, re.M)
        assert name in variables


def test_discover_workers_default_to_the_designs_32_and_match_the_discovery_module():
    bundle = (ROOT / "databricks.yml").read_text()
    assert re.search(r'^  discover_workers:\n    description: .*\n    default: "32"$', bundle, re.M)
    assert discovery.DEFAULT_WORKERS == 32
    assert re.search(r'^  max_queries_per_source:\n    description: .*\n    default: "0"$', bundle, re.M)
