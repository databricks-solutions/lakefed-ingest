"""The product identity that attributes the tool's usage (README "Usage attribution")."""
import re
from pathlib import Path

import pytest

import product

ROOT = Path(__file__).resolve().parents[1]


def test_engine_and_app_identify_as_the_same_product_and_version():
    app = (ROOT / "app" / "src" / "app_product.py").read_text()
    assert re.search(r'^PRODUCT_NAME = "([^"]+)"$', app, re.M).group(1) == product.PRODUCT_NAME == "ingestion-workbench"
    assert re.search(r'^PRODUCT_VERSION = "([^"]+)"$', app, re.M).group(1) == product.PRODUCT_VERSION


def test_every_sdk_client_the_engine_makes_identifies_itself():
    # A plain WorkspaceClient() would send no product: every client goes through product.workspace_client.
    offenders = []
    for path in list((ROOT / "src" / "lakefed_ingest").glob("*.py")) + list((ROOT / "src" / "lakefed_ingest").glob("*.ipynb")):
        if path.name != "product.py" and re.search(r"WorkspaceClient\(", path.read_text()):
            offenders.append(path.name)
    assert offenders == []


def test_workspace_client_passes_the_product(monkeypatch):
    import sys, types
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    monkeypatch.setitem(sys.modules, "databricks.sdk", types.SimpleNamespace(WorkspaceClient=FakeClient))
    product.workspace_client(host="h")
    assert seen == {"product": "ingestion-workbench", "product_version": product.PRODUCT_VERSION, "host": "h"}


def test_every_job_carries_the_project_tag():
    # Serverless jobs have no cluster to tag, so the job's own tag is what attributes their usage.
    yaml = pytest.importorskip("yaml")
    jobs = {}
    for path in list((ROOT / "resources").glob("*.yml")) + [ROOT / "app" / "resources" / "lakefed_ingest_app.yml"]:
        jobs.update((yaml.safe_load(path.read_text()).get("resources") or {}).get("jobs") or {})
    assert len(jobs) >= 7
    assert {name: (job.get("tags") or {}).get("project") for name, job in jobs.items()} == \
        {name: "${var.project_tag}" for name in jobs}


def test_every_bundle_resource_tag_names_the_product():
    for path in list((ROOT / "resources").glob("*.yml")) + [ROOT / "app" / "resources" / "lakefed_ingest_app.yml"]:
        text = path.read_text()
        assert "project: lakefed_ingest" not in text and "value: lakefed_ingest\n" not in text, path.name
    # One bundle (the app's resources are included from the root), so one variable sets every tag.
    assert re.search(r"^  project_tag:\n(?:    .*\n)+?    default: ingestion-workbench$", (ROOT / "databricks.yml").read_text(), re.M)
