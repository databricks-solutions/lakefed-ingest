"""The ingestion job's parameters line up: every widget the notebook reads is passed by the job task
and declared as a job parameter, and each tuning parameter defaults to the bundle variable of the same
name (the collection and the Lakebase names have their own defaults)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWN_DEFAULTS = {"task_collection", "db_instance", "db_name"}


def _widgets():
    notebook = json.loads((ROOT / "src/lakefed_ingest/ingest.ipynb").read_text())
    code = "\n".join(c["source"] if isinstance(c["source"], str) else "".join(c["source"])
                     for c in notebook["cells"] if c["cell_type"] == "code")
    return set(re.findall(r'widget\("([a-z_]+)"', code))


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


def test_table_retries_are_off_by_default():
    bundle = (ROOT / "databricks.yml").read_text()
    assert re.search(r'^  table_retries:\n    description: .*\n    default: "0"$', bundle, re.M)
