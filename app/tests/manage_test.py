"""Manage (main.py, config_import.py): only those with Can Manage on the app itself see it or reach its endpoints; its
export writes the jobs' tables in the columns Create from one CSV reads, page by page, and reads back as the same
tables; one import makes one job, so a file of several jobs is refused."""
import csv
import io
import json

import pytest
from pathlib import Path

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import config_import as ci  # noqa: E402
import main  # noqa: E402
import new_job as nj  # noqa: E402
import queries as q  # noqa: E402

USER = {"X-Forwarded-Email": "op@example.com"}


def control(id, group, database, table, **over):
    base = dict(id=id, task_collection=group, src_connection="sql_a", src_host=None, src_port=None, secret_key=None,
                src_database=database, src_schema="dbo", src_table=table, select_list="id, amount", primary_key="id",
                cursor_column=None, client_id=None, load_type="ct", client_attrs=None)
    return {**base, **over}


ROWS = [control(1, "sales_1", "Sales", "Orders", client_id="s1", client_attrs=json.dumps({"region": "North",
                                                                                          "tier": 2})),
        control(2, "sales_1", "Sales", "Lines", load_type="full", select_list=None),
        control(3, "hr_1", "People", "Staff", cursor_column="modified_at", load_type="cursor")]
MEMBERS = [{"id": 7, "name": "Sales", "task_collection": "sales_1"}, {"id": 8, "name": "HR", "task_collection": "hr_1"}]


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: "CAN_MANAGE")


class _Lakebase:
    """The export's reads: the jobs' groups, the identity column names, whether any table is read by host, and the
    pages of control rows after an id."""

    def __init__(self, rows, members):
        self.rows, self.members, self.pages = rows, members, 0

    def query(self, sql, params=()):
        if sql == q.export_groups_sql():
            ids = params[0]
            return [m for m in self.members if ids is None or m["id"] in ids]
        groups = set(params[0])
        mine = [r for r in self.rows if r["task_collection"] in groups]
        if sql == q.export_attr_names_sql():
            return [{"name": n} for n in sorted({k for r in mine for k in json.loads(r["client_attrs"] or "{}")})]
        if sql == q.export_reads_jdbc_sql():
            return [{"jdbc": any(r["src_host"] or r["src_port"] or r["secret_key"] for r in mine)}]
        assert sql == q.export_page_sql()
        self.pages += 1
        after, limit = params[1], params[2]
        return [r for r in mine if r["id"] > after][:limit]


def _export(rows, attrs=("region", "tier")):
    columns = ci.export_columns(any(r["src_host"] or r["src_port"] or r["secret_key"] for r in rows))
    job = {"sales_1": "Sales", "hr_1": "HR"}
    return ci.export_header(columns, list(attrs)) + ci.export_lines(
        [{**r, ci.JOB_COLUMN: job[r["task_collection"]]} for r in rows], columns, list(attrs))


def test_only_can_manage_on_the_app_manages_it(monkeypatch):
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    levels = {"admin@example.com": "CAN_MANAGE", "owner@example.com": "IS_OWNER", "op@example.com": "CAN_USE"}
    monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: levels.get(user))
    assert main._manage_refusal("admin@example.com") is None and main._manage_refusal("owner@example.com") is None
    assert main._manage_refusal("op@example.com") == (
        "managing the app needs Can Manage on the app lakefed-ops, or a place in its managers (the bundle's "
        "app_managers); you have Can Use")
    assert main._manage_refusal("nobody@example.com") == (
        "managing the app needs Can Manage on the app lakefed-ops, or a place in its managers (the bundle's "
        "app_managers); you have no access to it")
    assert main._manage_refusal(" ") == main.NO_IDENTITY


def test_no_answer_from_databricks_or_no_app_name_means_no(monkeypatch):
    monkeypatch.setattr(main.jobs, "APP_NAME", None)
    monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: pytest.fail("asked Databricks"))
    assert "DATABRICKS_APP_NAME" in main._manage_refusal("op@example.com")
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")

    def refused(user):
        raise PermissionError("Forbidden. The service principal can't read the app's permissions")
    monkeypatch.setattr(main.jobs, "user_app_permission", refused)
    assert main._manage_refusal("op@example.com") == "can't read your permission on the app lakefed-ops: Forbidden"


def test_the_app_acl_is_read_from_the_apps_permissions_and_admins_manage_it(monkeypatch):
    calls = []

    class Permissions:
        def get(self, kind, name):
            calls.append((kind, name))
            entries = [{"group_name": "admins", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
                       {"group_name": "users", "all_permissions": [{"permission_level": "CAN_USE"}]}]
            return type("Acl", (), {"access_control_list": [type("E", (), {"as_dict": lambda self, e=e: e})()
                                                            for e in entries]})()
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    monkeypatch.setattr(main.jobs, "workspace", lambda: type("W", (), {"permissions": Permissions()})())
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: ["admins"] if user == "admin@example.com" else [])
    monkeypatch.setattr(main.jobs, "_permission_cache", {})
    assert main.jobs.user_app_permission("admin@example.com") == "CAN_MANAGE"
    assert calls == [], "a workspace admin manages every app, so the list isn't read"
    assert main.jobs.user_app_permission("op@example.com") == "CAN_USE"
    assert main.jobs.user_app_permission("other@example.com") == "CAN_USE"
    assert calls == [("apps", "lakefed-ops")], "the access list is read once, then cached"



def _refusing_acl(monkeypatch):
    """The deployed app's case: its service principal may not read its own app's access list."""
    class Permissions:
        def get(self, kind, name):
            raise PermissionError("User 1 does not have apps.ruleSets/get on workspaces/1/apps/2/ruleSets/default")
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    monkeypatch.setattr(main.jobs, "workspace", lambda: type("W", (), {"permissions": Permissions()})())
    monkeypatch.setattr(main.jobs, "_permission_cache", {})


def test_a_listed_manager_manages_the_app_when_its_access_list_cant_be_read(monkeypatch):
    _refusing_acl(monkeypatch)
    monkeypatch.setattr(main.jobs, "APP_MANAGERS", {"owner@example.com", "ops-leads"})
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: ["ops-leads"] if user == "lead@example.com" else [])
    assert main._manage_refusal("Owner@Example.com") is None, "the deployer, listed by default, manages it"
    assert main._manage_refusal("lead@example.com") is None, "a member of a listed group manages it"


def test_an_unlisted_user_is_refused_plainly_when_the_access_list_cant_be_read(monkeypatch):
    _refusing_acl(monkeypatch)
    monkeypatch.setattr(main.jobs, "APP_MANAGERS", {"owner@example.com"})
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: [])
    refusal = main._manage_refusal("op@example.com")
    assert refusal.endswith("app_managers); you have Can Use") and "ruleSets" not in refusal


def test_the_bundle_lists_the_deployer_as_the_apps_manager_by_default():
    bundle = (Path(main.__file__).parent.parent / "databricks.yml").read_text()
    app = (Path(main.__file__).parent.parent / "resources" / "lakefed_ingest_app.yml").read_text()
    assert "app_managers:" in bundle and "default: ${workspace.current_user.userName}" in bundle
    assert "- name: APP_MANAGERS\n            value: ${var.app_managers}" in app

def test_every_manage_endpoint_refuses_those_who_dont_manage_the_app(monkeypatch):
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: "CAN_USE")
    monkeypatch.setattr(main.db, "query", lambda *a: pytest.fail("read Lakebase"))
    client = TestClient(main.app, headers=USER)
    for path in ("/api/manage/jobs", "/api/manage/version", "/api/manage/export.csv", "/api/manage/export.csv?jobs=7"):
        r = client.get(path)
        assert r.status_code == 403 and "Can Manage on the app lakefed-ops" in r.json()["detail"], path


def test_the_jobs_list_names_each_databricks_job_with_its_link(monkeypatch, manager):
    for variable, job_id in (("INGEST_JOB_ID", "11"), ("CHECK_JOB_ID", "12"), ("DISCOVER_JOB_ID", None),
                             ("CLUSTER_COPIES_JOB_ID", None), ("DELETE_JOB_ID", "15"), ("SETUP_JOB_ID", "16")):
        monkeypatch.setattr(main.jobs, variable, job_id)
    monkeypatch.setattr(main.jobs, "workspace_url", lambda: "https://ws.example.com")

    def summary(job_id):
        if job_id == "15":
            raise RuntimeError("Job 15 does not exist. Check the id")
        return {"name": f"[dev] lakefed_ingest {job_id}"}
    monkeypatch.setattr(main.jobs, "job_summary", summary)
    listed = TestClient(main.app, headers=USER).get("/api/manage/jobs").json()
    assert [(j["role"], j["job_id"], j["name"], j["url"]) for j in listed] == [
        ("Ingestion", 11, "[dev] lakefed_ingest 11", "https://ws.example.com/jobs/11"),
        ("Check", 12, "[dev] lakefed_ingest 12", "https://ws.example.com/jobs/12"),
        ("Delete", 15, None, "https://ws.example.com/jobs/15"),
        ("Schema setup", 16, "[dev] lakefed_ingest 16", "https://ws.example.com/jobs/16")]
    assert listed[2]["error"] == "Job 15 does not exist"


def test_the_version_is_what_the_deploy_passed_and_the_apps_active_deployment(monkeypatch, manager):
    monkeypatch.setattr(main.jobs, "APP_VERSION", "0123456789abcdef")
    monkeypatch.setattr(main, "_schema_state", lambda: {"database": 13, "app": 13})
    monkeypatch.setattr(main.jobs, "app_deployment", lambda: {"id": "d1", "created": "2026-10-30T11:00:00Z",
                                                              "creator": "deployer@example.com"})
    client = TestClient(main.app, headers=USER)
    assert client.get("/api/manage/version").json() == {
        "app": "lakefed-ops", "version": "0123456789abcdef", "error": None, "schema": {"database": 13, "app": 13},
        "deployment": {"id": "d1", "created": "2026-10-30T11:00:00Z", "creator": "deployer@example.com"}}

    def refused():
        raise PermissionError("Forbidden. No access to the app")
    monkeypatch.setattr(main.jobs, "app_deployment", refused)
    monkeypatch.setattr(main.jobs, "APP_VERSION", None)
    answer = client.get("/api/manage/version").json()
    assert (answer["version"], answer["deployment"], answer["error"]) == (None, {}, "Forbidden")


def test_the_permissions_say_who_manages_the_app(monkeypatch, manager):
    monkeypatch.setattr(main, "_permission_refusals", lambda *a, **k: {"run": None, "change": None, "delete": None})
    monkeypatch.setattr(main, "_databricks_job_refusals", lambda *a: {"run": None, "change": None, "delete": None})
    monkeypatch.setattr(main.db, "query", lambda *a: [])
    monkeypatch.setattr(main.jobs, "schedule_variants", lambda: {})
    assert TestClient(main.app, headers=USER).get("/api/permissions").json()["manage"] is None


def test_the_export_streams_every_jobs_tables_page_by_page(monkeypatch, manager):
    fake = _Lakebase(ROWS, MEMBERS)
    monkeypatch.setattr(main.db, "query", fake.query)
    monkeypatch.setattr(q, "EXPORT_PAGE", 2)
    r = TestClient(main.app, headers=USER).get("/api/manage/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="ingestion-jobs.csv"'
    assert r.text == _export(ROWS)
    assert fake.pages == 3, "two full pages, then an empty one ends it"
    lines = list(csv.DictReader(io.StringIO(r.text)))
    assert [(line["ingestion_job"], line["src_table"]) for line in lines] == [
        ("Sales", "Orders"), ("Sales", "Lines"), ("HR", "Staff")]
    assert "task_collection" not in lines[0] and "src_host" not in lines[0]


def test_an_export_of_one_job_is_named_after_it_and_holds_its_tables_only(monkeypatch, manager):
    monkeypatch.setattr(main.db, "query", _Lakebase(ROWS, MEMBERS).query)
    client = TestClient(main.app, headers=USER)
    r = client.get("/api/manage/export.csv?jobs=8")
    assert r.headers["content-disposition"] == 'attachment; filename="hr.csv"'
    assert r.text == _export(ROWS[2:], attrs=())
    assert client.get("/api/manage/export.csv?jobs=7,x").status_code == 422
    nothing = client.get("/api/manage/export.csv?jobs=99")
    assert nothing.status_code == 200 and nothing.text == ci.export_header(ci.export_columns(False), [])


def test_tables_read_by_host_and_secret_keep_those_columns():
    rows = [control(1, "sales_1", "Sales", "Orders", src_connection=None, src_host="sql1.example.com",
                    src_port=1433, secret_key="sales")]
    lines = list(csv.DictReader(io.StringIO(_export(rows, attrs=()))))
    assert (lines[0]["src_host"], lines[0]["src_port"], lines[0]["secret_key"], lines[0]["src_connection"]) == (
        "sql1.example.com", "1433", "sales", "")


def test_one_jobs_export_reads_back_as_the_same_tables_through_create_from_one_csv():
    text = _export(ROWS[:2])
    upload = nj.read_upload(text, "sql_a")
    assert upload["errors"] == []
    assert [(u["src_database"], u["src_table"], u["load_type"], u["fields"]["select_list"], u["fields"]["client_id"],
             u["fields"]["attr:region"], u["fields"]["attr:tier"]) for u in upload["lines"]] == [
        ("Sales", "Orders", "ct", "id, amount", "s1", "North", "2"), ("Sales", "Lines", "full", "", "", "", "")]

    plain, load_types = nj._line_load_types(text)
    read = ci.plan_import(plain, "_new", dict(src_type="sqlserver", use_remote_query=True, load_type="ct",
                                              write_mode="merge", fan_in=True, sink_catalog="c", sink_schema="s",
                                              sink_table="{table}", freshness_sla_minutes=None), [], check_rows=False)
    assert read["errors"] == [] and load_types == {2: "ct", 3: "full"}
    made = [line["row"] for line in read["lines"]]
    assert [(m["src_connection"], m["src_database"], m["src_schema"], m["src_table"], m["select_list"],
             m["primary_key"], m["client_id"], m["client_attrs"]) for m in made] == [
        ("sql_a", "Sales", "dbo", "Orders", "id, amount", "id", "s1", json.dumps({"region": "North", "tier": "2"})),
        ("sql_a", "Sales", "dbo", "Lines", None, "id", None, json.dumps({"region": None, "tier": None}))]
    # A table without identity columns, in a file where others have them, comes back with them blank, as the import
    # reads any blank identity cell.


def test_a_file_of_several_jobs_is_refused_as_one_import():
    text = ("ingestion_job,src_connection,src_database,src_schema,src_table,primary_key\n"
            "Sales,sql_a,Sales,dbo,Orders,id\n"
            "HR,sql_a,People,dbo,Staff,id\n")
    assert ci.plan_import(text, "g1", {}, [])["errors"] == [
        "this file holds 2 ingestion jobs (HR, Sales): one import makes one job, so import each job's lines on "
        "their own"]
    one = ci.plan_import(text.replace("HR,", "Sales,"), "g1", {}, [])
    assert not any("ingestion jobs" in e for e in one["errors"])
