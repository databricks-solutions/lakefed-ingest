"""Permission checks fail closed (main.py, jobs.py): no user from the Apps proxy means no access and no write; the
SCIM lookup's filter can't be bent by the user value; nested groups count, within caps; a change to a job's tables
needs Can Manage on every job that runs the groups it touches; and the Tables page checks before it locks rows."""
import contextlib
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import jobs  # noqa: E402
import main  # noqa: E402
import queries as q  # noqa: E402

ACL = [{"group_name": "users", "all_permissions": [{"permission_level": "CAN_VIEW"}]},
       {"group_name": "data-eng", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
       {"service_principal_name": "app-sp", "all_permissions": [{"permission_level": "CAN_MANAGE"}]},
       {"user_name": "owner@example.com", "all_permissions": [{"permission_level": "IS_OWNER"}]}]


def test_no_user_has_no_level_even_where_groups_and_service_principals_have_one():
    assert jobs.highest_permission(ACL, "", ["admins"]) is None
    assert jobs.highest_permission(ACL, "   ", []) is None
    assert jobs.highest_permission(ACL, None, []) is None
    assert jobs.highest_permission(ACL, "someone@example.com", []) == "CAN_VIEW"


def test_no_user_is_refused_every_action_without_asking_databricks(monkeypatch):
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: pytest.fail("asked Databricks"))
    assert set(main._permission_refusals({"job_variant": "default"}, "").values()) == {main.NO_IDENTITY}
    assert set(main._databricks_job_refusals(555, " ").values()) == {main.NO_IDENTITY}
    assert main._groups_refusal(["c1"], "") == main.NO_IDENTITY


def test_a_job_with_its_ingestion_job_is_checked_with_databricks_and_an_orphan_only_by_admins(monkeypatch):
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: [])
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
    assert main._permission_refusals({"job_variant": "default"}, "op@example.com")["delete"] is None
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {})
    orphan = main._permission_refusals({"job_variant": "default"}, "op@example.com")
    assert "only a workspace admin can change or delete it" in orphan["delete"]
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: ["admins"])
    assert main._permission_refusals({"job_variant": "default"}, "op@example.com")["delete"] is None


@pytest.mark.parametrize("action", ["restore", "purge"])
def test_restore_and_delete_now_ask_for_the_same_right_as_delete(monkeypatch, action):
    deleted_job = {"id": 7, "name": "Kept", "job_variant": "default", "deleted_at": "2026-10-30T12:00:00+00:00",
                   "options": {}}
    monkeypatch.setattr(main, "_load_schedule", lambda sid, cur=None: (deleted_job, []))
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: [])
    monkeypatch.setattr(main.db, "transaction", lambda: pytest.fail("opened a transaction"))
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {})
    operator = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    refused = operator.post(f"/api/schedules/7/{action}")
    assert refused.status_code == 403 and "only a workspace admin can change or delete it" in refused.json()["detail"]


def test_a_write_without_a_user_is_refused_before_it_runs(monkeypatch):
    monkeypatch.setattr(main.db, "transaction", lambda: pytest.fail("opened a transaction"))
    anonymous = TestClient(main.app)
    for path, body in (("/api/tables/enable", {"ids": [1], "enabled": False}), ("/api/ingestion-jobs", {"name": "x", "csv": ""}),
                       ("/api/schedules/1/delete", {})):
        r = anonymous.post(path, json=body)
        assert r.status_code == 403 and r.json()["detail"] == main.NO_IDENTITY
    blank = TestClient(main.app, headers={"X-Forwarded-Email": "  "})
    assert blank.post("/api/tables/reseed", json={"ids": [1]}).status_code == 403


def test_the_scim_filter_quotes_the_user_so_it_cant_change_the_query():
    assert jobs.scim_string('a@example.com') == '"a@example.com"'
    assert jobs.scim_string('x" or userName pr or "') == '"x\\" or userName pr or \\""'
    assert jobs.scim_string("back\\slash") == '"back\\\\slash"'


class _Directory:
    """Users with direct groups, and groups with the groups they are in, as the SCIM API answers."""

    def __init__(self, direct, parents, failing=()):
        self.direct, self.parents, self.failing, self.filters = direct, parents, set(failing), []
        self.users = SimpleNamespace(list=self.list_users)
        self.groups = SimpleNamespace(get=self.get_group)

    def list_users(self, filter, attributes):
        self.filters.append(filter)
        return [SimpleNamespace(groups=[SimpleNamespace(display=g, value=g) for g in self.direct])]

    def get_group(self, group_id):
        if group_id in self.failing:
            raise RuntimeError("503")
        return SimpleNamespace(groups=[SimpleNamespace(display=p, value=p) for p in self.parents.get(group_id, [])])


@pytest.fixture
def directory(monkeypatch):
    jobs._permission_cache.clear()

    def use(direct, parents, failing=()):
        found = _Directory(direct, parents, failing)
        monkeypatch.setattr(jobs, "workspace", lambda: found)
        return found
    yield use
    jobs._permission_cache.clear()


def test_a_grant_through_a_subgroup_counts(directory):
    found = directory(["team-x"], {"team-x": ["data-eng"], "data-eng": ["engineering"]})
    assert jobs.user_groups("u@example.com") == ["data-eng", "engineering", "team-x"]
    assert found.filters == ['userName eq "u@example.com"']
    assert jobs.highest_permission(ACL, "u@example.com", jobs.user_groups("u@example.com")) == "CAN_MANAGE"


def test_nested_groups_stop_at_the_caps_and_a_failed_lookup_only_takes_access_away(directory, monkeypatch):
    chain = {f"g{i}": [f"g{i + 1}"] for i in range(20)}
    directory(["g0"], chain)
    assert jobs.user_groups("deep@example.com") == [f"g{i}" for i in range(jobs.NESTED_GROUP_DEPTH + 1)]
    jobs._permission_cache.clear()
    directory(["team-x"], {"team-x": ["data-eng"]}, failing={"team-x"})
    assert jobs.user_groups("u@example.com") == ["team-x"]
    assert jobs.user_groups("") == []


def test_a_change_to_a_jobs_tables_needs_every_job_that_shares_its_groups(monkeypatch):
    schedules = [{"id": 1, "name": "Hourly", "job_variant": "default"}, {"id": 2, "name": "Nightly", "job_variant": "large"}]
    members = [{"schedule_id": 1, "task_collection": "calib_test", "removed_at": None},
               {"schedule_id": 2, "task_collection": "calib_test", "removed_at": None}]
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): schedules if sql == q.schedules_sql()
                        else members if sql == q.schedule_members_sql() else [])
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555", "large": "777"})
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE" if str(job_id) == "555" else "CAN_VIEW")
    request = SimpleNamespace(headers={"X-Forwarded-Email": "ops@example.com"})
    main._refuse_job(schedules[0], request, "change")          # Hourly alone allows it
    with pytest.raises(HTTPException) as refused:
        main._refuse_job_tables(schedules[0], ["calib_test"], request)
    assert refused.value.status_code == 403 and "ingestion job Nightly" in refused.value.detail


@pytest.mark.parametrize("path, body", [
    ("/api/ingestion-jobs/1/settings", {"settings": {"fan_in": True}, "dry_run": False}),
    ("/api/ingestion-jobs/1/enable", {"enabled": False}),
    ("/api/ingestion-jobs/1/tables/edit", {"schema": "dbo", "table": "t", "changes": {"load_type": "full"}, "dry_run": False}),
    ("/api/ingestion-jobs/1/databases", {"databases": [{"server": "s", "database": "d"}], "dry_run": False}),
    ("/api/ingestion-jobs/1/tables", {"csv": "src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
                                             "s,d,dbo,t,id,id\n", "dry_run": False}),
])
def test_every_job_edit_write_checks_the_groups_it_touches_before_any_lock(monkeypatch, path, body):
    checked = []
    monkeypatch.setattr(main, "_job_groups", lambda job_id: ["c1"])
    monkeypatch.setattr(main, "_load_schedule", lambda job_id, cur=None: ({"id": 1, "name": "Hourly", "job_variant": "default",
                                                                         "cadence_minutes": 60, "stagger": True}, []))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [])
    monkeypatch.setattr(main.js, "plan_databases", lambda *a, **k: {"errors": [], "new": [], "enable": [], "disable": [],
                                                               "new_groups": []})
    monkeypatch.setattr(main.nj, "plan_for_job", lambda *a, **k: {"errors": [], "groups": ["c1"], "new_groups": [], "lines": []})

    def refuse(job, groups, request):
        checked.append(sorted(groups))
        raise HTTPException(403, "no")
    monkeypatch.setattr(main, "_refuse_job_tables", refuse)
    monkeypatch.setattr(main.db, "transaction", lambda: pytest.fail("locked before the permission check"))
    r = TestClient(main.app, headers={"X-Forwarded-Email": "ops@example.com"}).post(path, json=body)
    assert r.status_code == 403 and checked and "c1" in checked[0]


def test_new_groups_from_add_databases_are_checked_too(monkeypatch):
    checked = []
    monkeypatch.setattr(main, "_job_groups", lambda job_id: ["hourly_s_c01"])
    monkeypatch.setattr(main, "_load_schedule", lambda job_id, cur=None: ({"id": 1, "name": "Hourly", "job_variant": "default",
                                                                         "cadence_minutes": 60, "stagger": True}, []))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [])
    monkeypatch.setattr(main.js, "plan_databases", lambda *a, **k: {"errors": [], "new": [], "enable": [], "disable": [],
                                                               "new_groups": ["hourly_s_c02"]})

    def refuse(job, groups, request):
        checked.append(sorted(groups))
        raise HTTPException(403, "no")
    monkeypatch.setattr(main, "_refuse_job_tables", refuse)
    r = TestClient(main.app, headers={"X-Forwarded-Email": "ops@example.com"}).post(
        "/api/ingestion-jobs/1/databases", json={"databases": [{"server": "s", "database": "d"}], "dry_run": False})
    assert r.status_code == 403 and checked == [["hourly_s_c01", "hourly_s_c02"]]


@pytest.mark.parametrize("path, body", [("/api/tables/enable", {"ids": [7], "enabled": False}),
                                        ("/api/tables/reseed", {"ids": [7]}),
                                        ("/api/tables/edit", {"ids": [7], "changes": {"load_type": "full"}, "dry_run": False})])
def test_the_tables_page_checks_permission_before_it_locks_rows(monkeypatch, path, body):
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"task_collection": "c1"}] if sql == q.control_groups_sql() else [])
    monkeypatch.setattr(main, "_refuse_groups_outside_jobs", lambda groups: None)
    monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: f"no Can Manage for {sorted(g for g in groups if g)}")
    monkeypatch.setattr(main.db, "transaction", lambda: pytest.fail("locked before the permission check"))
    r = TestClient(main.app, headers={"X-Forwarded-Email": "ops@example.com"}).post(path, json=body)
    assert r.status_code == 403 and "c1" in r.json()["detail"]


def test_a_row_moved_between_the_check_and_its_lock_is_checked_again(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_groups_refusal", lambda groups, user: seen.append(sorted(groups)) or None)
    request = SimpleNamespace(headers={"X-Forwarded-Email": "ops@example.com"})
    main._recheck_locked_rows([{"task_collection": "c1"}, {"task_collection": "c9"}], {"c1"}, request)
    assert seen == [["c9"]]
    main._recheck_locked_rows([{"task_collection": "c1"}], {"c1"}, request)
    assert seen == [["c9"]]
