"""The app bundle's grant on the dedicated JDBC login scope: the app's service principal gets WRITE on that one scope,
never MANAGE, and the ingestion bundle grants the app nothing. The bundle declares no permission on the scope (a
permission naming the app's service principal fails at deploy, before the app exists); the grants job sets WRITE
through the SDK instead. Read as text, like the other bundle tests."""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_BUNDLE = REPO / "app"
RESOURCES = APP_BUNDLE / "resources" / "lakefed_ingest_app.yml"
GRANT_NOTEBOOK = APP_BUNDLE / "notebooks" / "grant_app_access.ipynb"
ROOT_BUNDLE = REPO


def test_the_app_bundle_declares_the_login_scope_without_a_permissions_block():
    block = _secret_scopes_block(RESOURCES.read_text())
    assert block == (
        "  secret_scopes:\n"
        "    jdbc_logins:\n"
        "      name: ${var.jdbc_login_scope}\n"
        "      lifecycle:\n"
        "        prevent_destroy: true\n"
    )


def test_the_login_scope_block_names_no_principal_and_no_level():
    block = _secret_scopes_block(RESOURCES.read_text())
    assert "permissions:" not in block
    assert "level:" not in block
    assert "service_principal_name" not in block
    assert "user_name:" not in block and "group_name:" not in block


def test_nothing_in_the_app_bundle_grants_manage_on_a_secret_scope():
    for path in [RESOURCES, APP_BUNDLE / "databricks.yml"]:
        text = path.read_text()
        assert "level: MANAGE" not in text
        assert "permission: MANAGE" not in text


def test_the_app_has_no_secret_resource_of_its_own():
    assert "          secret:" not in RESOURCES.read_text()


def test_the_ingestion_bundle_grants_the_app_nothing_on_a_secret_scope():
    for path in [ROOT_BUNDLE / "databricks.yml", *sorted((ROOT_BUNDLE / "resources").glob("*.yml"))]:
        text = path.read_text()
        assert "service_principal_name" not in text
        assert "level: MANAGE" not in text


def test_the_scope_name_is_a_variable_and_the_app_is_told_it():
    variables = (APP_BUNDLE / "databricks.yml").read_text()
    assert "  jdbc_login_scope:\n" in variables
    assert "default: lakefed_ingest_jdbc_logins" in variables
    text = RESOURCES.read_text()
    assert "          - name: JDBC_LOGIN_SCOPE\n            value: ${var.jdbc_login_scope}\n" in text


def test_the_grants_job_is_passed_the_login_scope_name_from_the_bundle_variable():
    assert "              jdbc_login_scope: ${var.jdbc_login_scope}\n" in RESOURCES.read_text()


def test_the_grants_notebook_reads_the_login_scope_and_grants_the_app_write_on_it():
    source = _notebook_source()
    assert 'dbutils.widgets.text("jdbc_login_scope", "")' in source
    assert 'jdbc_login_scope = dbutils.widgets.get("jdbc_login_scope")' in source
    assert "login_scope_grants.grant_write(w, jdbc_login_scope, role)" in source


def test_the_login_scope_cannot_be_destroyed_by_accident():
    assert "prevent_destroy: true" in _secret_scopes_block(RESOURCES.read_text())


def _secret_scopes_block(text):
    start = text.index("\n  secret_scopes:\n") + 1
    return text[start:text.index("\n  jobs:\n", start)]


def _notebook_source():
    return "".join("".join(cell["source"]) for cell in json.loads(GRANT_NOTEBOOK.read_text())["cells"])
