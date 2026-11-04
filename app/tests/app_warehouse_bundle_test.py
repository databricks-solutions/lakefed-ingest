"""The bundle's app_warehouse_id: a variable, blank by default (the bundle's own warehouse), passed to the app as
APP_WAREHOUSE_ID beside the unchanged WAREHOUSE_ID from the discovery-warehouse resource, and to the grants job, which
gives the app CAN_USE on it. Read as text, like the other bundle tests."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VARIABLES = REPO / "databricks.yml"
RESOURCES = REPO / "app" / "resources" / "lakefed_ingest_app.yml"


def test_the_variable_exists_and_is_blank_by_default():
    text = VARIABLES.read_text()
    start = text.index("\n  app_warehouse_id:\n")
    block = text[start:text.index("\n  jdbc_login_scope:\n", start) + 1]
    assert block.endswith('    default: ""\n')
    description = " ".join(block.split())
    assert "Pro or serverless" in description and "2025.35 or above" in description
    assert "CAN_USE" in description and "lakefed_ingest_app_grants" in description


def test_the_app_is_told_the_variable_and_keeps_the_bundles_own_warehouse_from_its_resource():
    text = RESOURCES.read_text()
    assert "          - name: APP_WAREHOUSE_ID\n            value: id:${var.app_warehouse_id}\n" in text
    assert "          - name: WAREHOUSE_ID\n            value_from: discovery-warehouse\n" in text
    assert "            id: ${resources.sql_warehouses.lakefed_ingest_discovery.id}\n" in text


def test_no_app_env_entry_renders_blank_with_the_variables_at_their_defaults():
    # Databricks Apps refuses an env entry whose value renders blank ("Must specify environment variable source using
    # either value or valueFrom"; the 1 Nov deploy with app_warehouse_id blank). Render each entry with every variable
    # whose default is blank set to blank, and check it still carries a value.
    import re
    blank = set(re.findall(r'\n  ([a-z_]+):\n(?:    .*\n)*?    default: ""\n', VARIABLES.read_text()))
    assert "app_warehouse_id" in blank
    env = RESOURCES.read_text().split("        env:\n", 1)[1].split("      resources:\n", 1)[0]
    entries = re.findall(r"- name: (\S+)\n\s+(value|value_from): ?(.*)\n", env)
    assert len(entries) == env.count("- name: ") and entries
    for name, kind, raw in entries:
        rendered = re.sub(r"\$\{var\.([a-z_]+)\}", lambda m: "" if m.group(1) in blank else "x",
                          raw.strip().strip('"'))
        assert kind == "value_from" or rendered, f"{name} renders blank when its variable is at its blank default"


def test_the_grants_job_is_passed_the_variable():
    assert "              app_warehouse_id: ${var.app_warehouse_id}\n" in RESOURCES.read_text()


def test_the_header_lists_the_grant_on_a_warehouse_of_your_own():
    header = RESOURCES.read_text().split("\nresources:\n")[0]
    assert "app_warehouse_id" in header and "lakefed_ingest_app_grants" in header
