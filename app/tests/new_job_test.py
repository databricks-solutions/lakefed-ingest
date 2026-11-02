"""New ingestion job: the plan (new_job.py) from a CSV of where each table is and the job's settings, and its endpoint
(POST /api/ingestion-jobs) with a fake database. The SQL itself runs against a real Postgres in new_job_pg_test.py."""
import contextlib
import json
import re

import pytest

import job_settings as js
import new_job as nj
import queries as q
import table_removal

CSV = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
       "sql_a,eng_001,gen,t0001,\"id, amount\",id\n"
       "sql_a,eng_002,gen,t0001,\"id, amount\",id\n"
       "sql_a,eng_002,gen,t0002,\"id, qty\",id\n")
FAN_IN = {"fan_in": True, "sink_catalog": "main", "sink_schema": "engagements", "sink_table": "{table}",
          "identity": {"engagement": "{database}"}}
HEAD = "src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
PLAIN = {"sink_catalog": "main", "sink_schema": "s", "sink_table": "{database}_{table}"}
TABLE_ONLY = {"sink_catalog": "main", "sink_schema": "s", "sink_table": "{table}"}
NAMED = (HEAD + 'sql_a,eng_001,gen,Order-Lines,"id",id\n' + 'sql_a,eng_001,gen,"Sales (2024)","id",id\n'
         + "sql_a,eng_001,gen,a--b,\"id\",id\n")
# A few files with problems of every kind, planned as the pre-check version planned them: each line's status, errors
# (in order) and group, and the plan's errors and groups.
VALIDATION_CASES = {
    "clean": (HEAD + 'sql_a,eng_001,gen,t0001,"id, amount",id\nsql_a,eng_002,gen,t0001,"id, amount",id\n'
              + 'sql_a,eng_002,gen,t0002,"id, qty",id\n', FAN_IN, set()),
    "fan_in_problems": (
        "src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id,src_host\n"
        + 'sql_a,eng_001,gen,t1,"*",id,,\n'
        + 'sql_a,eng_002,gen,,"id",id,bad id!,\n'
        + 'sql_a,eng_003,gen,t3,"id",,,\n'
        + 'sql_a,eng_004,gen,t5,"id",id,,host1\n'
        + 'sql_a,eng_004,gen,t5,"id",id,,\n', FAN_IN, set()),
    "no_fan_in_problems": (
        "src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id,task_collection,"
        "src_port,load_type\n"
        + 'sql_a,eng_001,gen,t1,"id",id,x,other_group,70000,bogus\n'
        + 'sql_a,eng_002,gen,t2,"id",id,,,,full\n'
        + 'sql_a,eng_003,gen,t3,"id, a",id,,,,cursor\n'
        + 'sql_a,eng_004,gen,t4,"id",id,,,,full,extra\n', PLAIN, set()),
    "taken_group": (
        HEAD + 'sql_a,eng_001,gen,t1,"id",id\nsql_a,eng_002,gen,t2,"id",id\nsql_a,eng_002,gen,t2,"id",id\n', PLAIN,
        {"job_sql_a_c01", "job_sql_a_c02", "job_sql_a_c03", "job_sql_a_c04"}),
}
VALIDATION_EXPECTED = {
    "clean": {
        "errors": [],
        "groups": ['job_sql_a_c01', 'job_sql_a_c02'],
        "lines": [
            (2, 'eng_001.gen.t0001', 'valid',
             [],
             'job_sql_a_c02', 'main.engagements.t0001'),
            (3, 'eng_002.gen.t0001', 'valid',
             [],
             'job_sql_a_c01', 'main.engagements.t0001'),
            (4, 'eng_002.gen.t0002', 'valid',
             [],
             'job_sql_a_c01', 'main.engagements.t0002'),
        ],
    },
    "fan_in_problems": {
        "errors": [],
        "groups": ['job_host1_1433_c01', 'job_sql_a_c01', 'job_sql_a_c02', 'job_sql_a_c03', 'job_sql_a_c04'],
        "lines": [
            (2, 'eng_001.gen.t1', 'invalid',
             [
                 "select_list must list explicit columns (* isn't supported)",
             ],
             'job_sql_a_c01', 'main.engagements.t1'),
            (3, 'eng_002.gen.', 'invalid',
             [
                 'src_table is required',
                 'sink_table is required',
                 'client_id may only use letters, digits and _ . : - (1-128 characters)',
             ],
             'job_sql_a_c02', 'main.engagements.'),
            (4, 'eng_003.gen.t3', 'invalid',
             [
                 'primary_key is required for load_type ct',
             ],
             'job_sql_a_c03', 'main.engagements.t3'),
            (5, 'eng_004.gen.t5', 'invalid',
             [
                 'give the UC connection or the host, not both',
                 'secret_key is required',
             ],
             'job_host1_1433_c01', 'main.engagements.t5'),
            (6, 'eng_004.gen.t5', 'invalid',
             [
                 'line 5 already writes c.s.t5_eng_004',
                 'line 5 already writes main.engagements.t5 for eng_004',
             ],
             'job_sql_a_c04', 'main.engagements.t5'),
        ],
    },
    "no_fan_in_problems": {
        "errors": [],
        "groups": ['job_sql_a_c01', 'job_sql_a_c02', 'job_sql_a_c03', 'job_sql_a_c04'],
        "lines": [
            (2, 'eng_001.gen.t1', 'invalid',
             [
                 'task_collection is other_group, but this import is into _new',
                 'src_port must be a whole number from 1 to 65535',
                 'load_type must be one of ct, cursor, full',
                 'client_id must be blank: this job has no fan-in',
             ],
             'job_sql_a_c01', 'main.s.eng_001_t1'),
            (3, 'eng_002.gen.t2', 'valid',
             [],
             'job_sql_a_c02', 'main.s.eng_002_t2'),
            (4, 'eng_003.gen.t3', 'invalid',
             [
                 'cursor_column is required for load_type cursor',
             ],
             'job_sql_a_c03', 'main.s.eng_003_t3'),
            (5, 'eng_004.gen.t4', 'invalid',
             [
                 'this line has more cells than the header has columns',
             ],
             'job_sql_a_c04', 'main.s.eng_004_t4'),
        ],
    },
    "taken_group": {
        "errors": [],
        "groups": ['job_sql_a_c01', 'job_sql_a_c02'],
        "lines": [
            (2, 'eng_001.gen.t1', 'invalid',
             [
                 'group job_sql_a_c02 is already in use: give the job another name',
             ],
             'job_sql_a_c02', 'main.s.eng_001_t1'),
            (3, 'eng_002.gen.t2', 'invalid',
             [
                 'group job_sql_a_c01 is already in use: give the job another name',
             ],
             'job_sql_a_c01', 'main.s.eng_002_t2'),
            (4, 'eng_002.gen.t2', 'invalid',
             [
                 'line 3 already writes c.s.t2_eng_002',
                 'group job_sql_a_c01 is already in use: give the job another name',
                 'line 3 already writes main.s.eng_002_t2: turn fan-in on, or put {database} in the target names',
             ],
             'job_sql_a_c01', 'main.s.eng_002_t2'),
        ],
    },
}


def test_each_line_becomes_a_row_with_the_jobs_settings_and_a_group_per_server():
    plan = nj.plan("Engagement extraction", CSV, FAN_IN, groups_per_server=4)
    assert plan["errors"] == [] and [line["status"] for line in plan["lines"]] == ["valid"] * 3
    rows = [line["row"] for line in plan["lines"]]
    assert {r["sink_schema"] + "." + r["sink_table"] for r in rows} == {"engagements.t0001", "engagements.t0002"}
    assert [(r["client_id"], json.loads(r["client_attrs"])) for r in rows[:2]] == [
        ("eng_001", {"engagement": "eng_001"}), ("eng_002", {"engagement": "eng_002"})]
    assert all(r["use_remote_query"] and r["load_type"] == "ct" and r["write_mode"] == "merge" for r in rows)
    # One database's tables are always in one group, named after the job and its server.
    assert rows[1]["task_collection"] == rows[2]["task_collection"]
    assert all(g.startswith("engagement_extraction_sql_a_c0") for g in plan["groups"])


def test_the_shared_and_legacy_keys_are_each_one_pinned_template():
    assert js.SHARED_CLIENT_ID == "{server}_{database}"
    assert js.LEGACY_CLIENT_ID == "{database}"


def test_a_new_fan_in_job_keys_each_row_by_its_server_and_database():
    plan = nj.plan("Engagement extraction", CSV, FAN_IN, groups_per_server=4, new_job=True)
    assert plan["errors"] == []
    assert [line["row"]["client_id"] for line in plan["lines"]] == ["sql_a_eng_001", "sql_a_eng_002", "sql_a_eng_002"]
    assert nj.settings_of({}, new_job=True)["client_id"] == "{server}_{database}"
    assert nj.settings_of({"client_id": " "}, new_job=True)["client_id"] == "{server}_{database}"


def test_a_job_made_before_keeps_the_database_as_its_key():
    plan = nj.plan("Engagement extraction", CSV, FAN_IN, groups_per_server=4)
    assert [line["row"]["client_id"] for line in plan["lines"]] == ["eng_001", "eng_002", "eng_002"]
    assert nj.settings_of({})["client_id"] == "{database}"


def test_a_new_key_the_rules_refuse_names_the_server_and_database():
    spaced = nj.plan("Engagement extraction", CSV.replace("eng_001", "eng 001", 1), FAN_IN, 4, new_job=True)
    assert spaced["lines"][0]["status"] == "invalid"
    assert spaced["lines"][0]["errors"] == [
        "server sql_a and database eng 001 would get the client_id 'sql_a_eng 001': letters, digits and _ . : - only, "
        "1 to 128 characters"]
    too_long = nj.plan("Engagement extraction", CSV.replace("eng_002", "x" * 123, 1), FAN_IN, 4, new_job=True)
    assert too_long["lines"][1]["status"] == "invalid" and "1 to 128 characters" in too_long["lines"][1]["errors"][0]


def test_a_named_instance_is_refused_with_its_reason_and_a_host_and_port_is_not():
    assert nj.key_errors("sql_a:1433", "eng_001", "sql_a:1433_eng_001") == []
    [message] = nj.key_errors("sql\\inst:1433", "eng_001", "sql\\inst:1433_eng_001")
    assert "sql\\inst:1433" in message and "named instance" in message


def test_a_client_id_the_file_gives_is_kept_as_typed_and_checked():
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id\n"
            'sql_a,eng_001,gen,t0001,"id, amount",id,custom_1\n'
            'sql_a,eng_002,gen,t0001,"id, amount",id,my key\n')
    plan = nj.plan("Engagement extraction", text, FAN_IN, 4, new_job=True)
    assert [line["row"]["client_id"] for line in plan["lines"]] == ["custom_1", "my key"]
    assert plan["lines"][0]["status"] == "valid"
    assert plan["lines"][1]["errors"] == [
        "server sql_a and database eng_002 would get the client_id 'my key': letters, digits and _ . : - only, "
        "1 to 128 characters"]


def test_the_readable_identity_columns_are_one_pinned_mapping():
    assert js.SHARED_IDENTITY == {"source_server": "{server}", "source_database": "{database}"}


def test_a_new_fan_in_job_gives_each_row_its_source_server_and_database_unless_they_are_removed():
    no_identity = {k: v for k, v in FAN_IN.items() if k != "identity"}
    plan = nj.plan("Engagement extraction", CSV, no_identity, 4, new_job=True)
    assert [json.loads(line["row"]["client_attrs"]) for line in plan["lines"]] == [
        {"source_server": "sql_a", "source_database": "eng_001"}, {"source_server": "sql_a", "source_database": "eng_002"},
        {"source_server": "sql_a", "source_database": "eng_002"}]
    removed = nj.plan("Engagement extraction", CSV, {**no_identity, "identity": {}}, 4, new_job=True)
    assert [line["row"]["client_attrs"] for line in removed["lines"]] == [None, None, None]
    assert nj.settings_of({"fan_in": True}, new_job=True)["identity"] == js.SHARED_IDENTITY
    assert nj.settings_of({"fan_in": True})["identity"] == {}


def test_a_new_jobs_file_may_not_set_the_readable_columns_it_fills():
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,attr:source_server\n"
            'sql_a,eng_001,gen,t0001,"id, amount",id,other\n')
    plan = nj.plan("Engagement extraction", text, {k: v for k, v in FAN_IN.items() if k != "identity"}, 4, new_job=True)
    assert plan["lines"][0]["status"] == "invalid"
    assert plan["lines"][0]["errors"] == ["client_attrs may not set source_server: the job fills it from the server and database"]


def test_add_tables_gives_a_new_jobs_rows_the_readable_columns_and_an_old_jobs_none():
    more = HEAD + 'sql_a,eng_009,gen,t0001,"id, amount",id\n'
    no_identity = {k: v for k, v in FAN_IN.items() if k != "identity"}
    new_rows = [dict(line["row"], id=i + 1, task_enabled=True)
                for i, line in enumerate(nj.plan("Engagement extraction", CSV, no_identity, 4, new_job=True)["lines"])]
    added = nj.plan_for_job("Engagement extraction", more, "ct", new_rows, 4)["lines"][0]["row"]
    assert json.loads(added["client_attrs"]) == {"source_server": "sql_a", "source_database": "eng_009"}
    old_rows = [dict(r, client_attrs=None) for r in _job_rows()]
    assert nj.plan_for_job("Engagement extraction", more, "ct", old_rows, 4)["lines"][0]["row"]["client_attrs"] is None


def test_tables_added_to_a_new_job_take_its_key_and_an_old_job_keeps_its_own():
    more = HEAD + 'sql_a,eng_009,gen,t0001,"id, amount",id\n'
    new_rows = [dict(line["row"], id=i + 1, task_enabled=True)
                for i, line in enumerate(nj.plan("Engagement extraction", CSV, FAN_IN, 4, new_job=True)["lines"])]
    assert nj.plan_for_job("Engagement extraction", more, "ct", new_rows, 4)["lines"][0]["row"]["client_id"] == "sql_a_eng_009"
    assert nj.plan_for_job("Engagement extraction", more, "ct", _job_rows(), 4)["lines"][0]["row"]["client_id"] == "eng_009"


def test_a_mistyped_setting_is_refused_and_the_words_for_yes_and_no_are_read():
    assert nj.plan("Job", CSV, {**FAN_IN, "sink_shema": "x"}, 4)["errors"] == ["unknown setting sink_shema"]
    assert nj.plan("Job", CSV, {**FAN_IN, "fan_in": "sometimes"}, 4)["errors"] == ["fan_in: 'sometimes' is not true or false"]
    off = nj.plan("Job", CSV, {**FAN_IN, "fan_in": "false", "sink_table": "{database}_{table}"}, 4)
    assert off["errors"] == [] and off["settings"]["fan_in"] is False
    assert all(line["row"]["client_id"] is None for line in off["lines"])
    assert nj.given_errors({"lowercase": "no", "sink_shema": "x"}) == ["unknown setting sink_shema"]


def test_a_retired_lowercase_setting_is_ignored_and_a_new_job_takes_the_unity_catalog_name():
    named = HEAD + 'sql_a,eng_001,gen,"Sales (2024)","id",id\n'
    sink = {"sink_catalog": "main", "sink_schema": "s", "sink_table": "{table}"}
    plain = nj.plan("Job", named, sink, 4, new_job=True)
    retired = nj.plan("Job", named, {**sink, "lowercase": "no"}, 4, new_job=True)
    assert retired["errors"] == [] and "lowercase" not in retired["settings"]
    assert [line["row"] for line in retired["lines"]] == [line["row"] for line in plain["lines"]]
    assert retired["lines"][0]["row"]["sink_table"] == q.unity_catalog_identifier("Sales (2024)")


def _server(databases: int, tables: int) -> str:
    return "src_connection,src_database,src_schema,src_table,select_list,primary_key\n" + "".join(
        f"sql_lab,eng_{d:03d},gen,t{t:04d},\"id, v\",id\n" for d in range(1, databases + 1) for t in range(1, tables + 1))


def test_a_whole_server_of_100000_tables_fits_one_job():
    plan = nj.plan("Big job", _server(100, 1000), {"fan_in": True, "sink_catalog": "main", "sink_schema": "s"}, 8, set())
    assert plan["errors"] == [] and len(plan["lines"]) == 100_000 and len(plan["groups"]) == 8


def test_more_lines_than_a_job_takes_at_once_are_refused(monkeypatch):
    monkeypatch.setattr(nj, "MAX_LINES", 10)
    plan = nj.plan("Big job", _server(1, 11), {"sink_catalog": "main", "sink_schema": "s"}, 4, set())
    assert plan["errors"] == ["more than 10 lines: at most 10 in one import"]


def test_import_csv_keeps_its_own_cap_of_5000_lines():
    import config_import as ci
    read = ci.plan_import(_server(1, 5001), "g", {"src_type": "sqlserver", "load_type": "ct", "use_remote_query": True,
                                                  "sink_catalog": "c", "sink_schema": "s", "sink_table": "{table}"}, [])
    assert read["errors"] == ["more than 5,000 lines: at most 5,000 in one import"]


def test_the_same_file_always_places_its_databases_the_same_way():
    first = [line["group"] for line in nj.plan("Job", CSV, FAN_IN, 4)["lines"]]
    assert first == [line["group"] for line in nj.plan("Job", CSV, FAN_IN, 4)["lines"]]


def test_a_files_databases_are_spread_evenly_by_tables_and_never_split():
    text = HEAD + "".join(f'sql_a,eng_{d:03d},gen,t{t:04d},"id",id\n' for d, tables in ((1, 6), (2, 3), (3, 3), (4, 2), (5, 2))
                          for t in range(1, tables + 1))
    plan = nj.plan("Job", text, FAN_IN, 2)
    groups = {}
    for line in plan["lines"]:
        groups.setdefault(line["row"]["src_database"], set()).add(line["group"])
    assert all(len(g) == 1 for g in groups.values())         # a database is never split
    tables = {}
    for line in plan["lines"]:
        tables[line["group"]] = tables.get(line["group"], 0) + 1
    assert sorted(tables.values()) == [8, 8]                 # 16 tables in two even groups


def test_by_default_a_file_takes_one_group_per_server_for_a_few_tables():
    assert nj.plan("Job", CSV, FAN_IN)["groups"] == ["job_sql_a_c01"]


def test_without_fan_in_two_databases_cannot_write_one_table():
    lines = nj.plan("Job", CSV, {**FAN_IN, "fan_in": False}, 4)["lines"]
    assert lines[1]["status"] == "invalid" and "turn fan-in on" in lines[1]["errors"][0]
    named = nj.plan("Job", CSV, {**FAN_IN, "fan_in": False, "sink_table": "{database}_{table}"}, 4)["lines"]
    assert [line["status"] for line in named] == ["valid"] * 3


def test_write_modes_follow_each_tables_load_type():
    plan = nj.plan("Job", CSV, {**FAN_IN, "load_type": "full", "write_mode_full": "append"}, 4)
    assert {line["row"]["write_mode"] for line in plan["lines"]} == {"append"}
    plan = nj.plan("Job", CSV, {**FAN_IN, "write_mode_changes": "append"}, 4)
    assert {line["row"]["write_mode"] for line in plan["lines"]} == {"append"}
    plan = nj.plan("Job", CSV, {**FAN_IN, "load_type": "cursor", "write_mode_changes": "append"}, 4)
    assert {line["row"]["write_mode"] for line in plan["lines"]} == {"merge"}     # cursor tables always merge


@pytest.mark.parametrize("given,error", [
    ({**FAN_IN, "sink_catalog": ""}, "set the target catalog"),
    ({**FAN_IN, "sink_table": "{database}_{table}"}, "{database}"),
    ({**FAN_IN, "load_type": "cdc"}, "load type must be one of"),
    ({**FAN_IN, "identity": {"9bad": "{database}"}}, "identity column 9bad"),
])
def test_settings_the_job_cannot_take_are_refused_before_any_line(given, error):
    plan = nj.plan("Job", CSV, given, 4)
    assert plan["lines"] == [] and any(error in e for e in plan["errors"])


def test_a_line_naming_both_a_connection_and_a_host_or_neither_is_invalid():
    text = ("src_connection,src_host,src_database,src_schema,src_table,select_list,primary_key\n"
            "sql_a,h1,eng_003,gen,t0001,id,id\n"
            ",,eng_004,gen,t0001,id,id\n")
    both, neither = nj.plan("Job", text, FAN_IN, 4)["lines"]
    assert both["status"] == "invalid" and "not both" in " ".join(both["errors"])
    assert neither["status"] == "invalid" and "src_host is required" in neither["errors"]


def test_a_group_already_in_use_refuses_the_line():
    taken = {nj.plan("Job", CSV, FAN_IN, 4)["lines"][0]["group"]}
    line = nj.plan("Job", CSV, FAN_IN, 4, taken)["lines"][0]
    assert line["status"] == "invalid" and "already in use" in line["errors"][-1]


@pytest.mark.parametrize("case", list(VALIDATION_EXPECTED))
def test_each_line_keeps_the_status_and_errors_it_had_when_a_row_was_checked_three_times(case):
    text, settings, taken = VALIDATION_CASES[case]
    plan = nj.plan("Job", text, settings, 4, taken)
    assert {"errors": plan["errors"], "groups": plan["groups"],
            "lines": [(line["line"], line["table"], line["status"], line["errors"], line["group"], line["sink"])
                      for line in plan["lines"]]} == VALIDATION_EXPECTED[case]


def test_each_row_is_checked_once_in_order_when_its_plan_is_made(monkeypatch):
    checked = []
    validate = nj.q.validate_control_row

    def counting(row):
        checked.append((row["src_database"], row["src_table"]))
        return validate(row)

    monkeypatch.setattr(nj.q, "validate_control_row", counting)
    plan = nj.plan("Engagement extraction", CSV, FAN_IN, groups_per_server=4)
    assert len(plan["lines"]) == 3
    assert checked == [("eng_001", "t0001"), ("eng_002", "t0001"), ("eng_002", "t0002")]


def test_the_summary_counts_tables_databases_and_groups_per_server():
    summary = nj.summary(nj.plan("Job", CSV, FAN_IN, 4))
    assert summary["tables"] == 3 and summary["invalid"] == []
    assert [(s["server"], s["databases"], s["tables"], s["read_through"]) for s in summary["servers"]] == [
        ("sql_a", 2, 3, "UC connection")]
    assert summary["servers"][0]["load_types"] == {"ct": 3}


def test_the_summary_leaves_out_of_its_counts_the_tables_the_job_removed_and_counts_them_apart():
    plan = nj.plan("Job", CSV, FAN_IN, 4)
    plan["lines"][1]["status"] = table_removal.LEFT_OUT
    summary = nj.summary(plan)
    assert (summary["tables"], summary["left_out"], summary["in_job"]) == (2, 1, 0)
    assert sum(s["tables"] for s in summary["servers"]) == 2


# ---- the endpoint (needs FastAPI and httpx) -----------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import main
    import queries as q
    from api_test import FakeCursor, FakeDB

    class Cursor(FakeCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if sql == q.insert_schedule_sql():
                self._rows = [(7,)]

    fake = FakeDB()
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"task_collection": "other"}]
                        if sql == q.collections_sql() else [{"id": 1, "made": True}] if sql == q.insert_task_sql() else [])
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): None)
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cursor(fake))))
    monkeypatch.setattr(main.jobs, "schedule_variants", lambda: {"default": "1", "check": "2"})
    synced = []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or [])
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db, c.synced, c.q = fake, synced, q
    return c


def _create(client, dry_run=True, **extra):
    return client.post("/api/ingestion-jobs", json={"name": "Engagement extraction", "csv": CSV, "settings": FAN_IN,
                                                    "dry_run": dry_run, **extra})


def _inserted_rows(client):
    return [dict(zip(client.q.CONTROL_INSERT_COLUMNS, p)) for p in client.db.of("insert into lakefed_ingest.control (")]


def test_a_preview_writes_nothing(client):
    body = _create(client).json()
    assert body["created"] is False and body["summary"]["tables"] == 3
    assert client.db.statements == [] and client.synced == []


def test_create_writes_the_job_its_tables_and_groups_in_one_go_then_syncs(client):
    body = _create(client, dry_run=False).json()
    assert body["created"] is True and body["id"] == 7 and client.synced == [7]
    schedule = client.db.of("insert into lakefed_ingest.schedule (")
    assert len(schedule) == 1 and schedule[0][0] == "Engagement extraction" and schedule[0][6] is True   # paused
    inserted = [dict(zip(client.q.CONTROL_INSERT_COLUMNS, p)) for p in client.db.of("insert into lakefed_ingest.control (")]
    assert [r["src_database"] for r in inserted] == ["eng_001", "eng_002", "eng_002"]
    assert [r["client_id"] for r in inserted] == ["sql_a_eng_001", "sql_a_eng_002", "sql_a_eng_002"]
    members = client.db.of("insert into lakefed_ingest.schedule_member")
    assert sorted(m[1] for m in members) == body["summary"]["groups"] and all(m[0] == 7 for m in members)
    assert client.db.of("select pg_try_advisory_xact_lock")


def test_a_csv_create_stores_unity_catalog_names_and_names_its_tables_by_them(client):
    assert _create(client, dry_run=False, csv=NAMED).json()["created"] is True
    [schedule] = client.db.of("insert into lakefed_ingest.schedule (")
    assert json.loads(schedule[8])["target_names"] == "unity_catalog"
    assert [r["sink_table"] for r in _inserted_rows(client)] == ["order-lines", "sales_(2024)", "a--b"]


def test_a_csv_create_keeps_a_typed_target_name_and_refuses_one_unity_catalog_refuses(client):
    one = HEAD + 'sql_a,eng_001,gen,Order-Lines,"id",id\n'
    assert _create(client, dry_run=False, csv=one, settings={**FAN_IN, "sink_table": "Sales-Orders"}).json()["created"]
    assert [r["sink_table"] for r in _inserted_rows(client)] == ["Sales-Orders"]
    refused = _create(client, dry_run=False, csv=one, settings={**FAN_IN, "sink_table": "Sales Orders"})
    assert refused.status_code == 422 and "Unity Catalog refuses" in refused.text
    assert len(_inserted_rows(client)) == 1


def test_create_is_refused_while_a_line_is_invalid(client):
    body = client.post("/api/ingestion-jobs", json={"name": "Job", "csv": CSV, "settings": {**FAN_IN, "fan_in": False},
                                                     "dry_run": False}).json()
    assert body["created"] is False and body["summary"]["invalid"]
    assert client.db.statements == []


def test_a_target_another_table_writes_refuses_the_whole_job(client):
    taken = CSV.replace("t0002", "lines")          # the fake database says the sink of a table named lines is taken
    r = client.post("/api/ingestion-jobs", json={"name": "Job", "csv": taken, "settings": FAN_IN, "dry_run": False})
    assert r.status_code == 409 and "already writes" in r.json()["detail"] and client.synced == []


def test_settings_or_a_schedule_it_cannot_take_are_422(client):
    r = _create(client, dry_run=False, settings={**FAN_IN, "sink_catalog": ""})
    assert r.status_code == 422 and "set the target catalog" in r.json()["detail"]["errors"]
    # A preview isn't refused for a blank target: it shows the tables and names what Create needs.
    r = _create(client, settings={**FAN_IN, "sink_catalog": ""})
    assert r.status_code == 200 and r.json()["target_missing"] == ["target catalog"] and r.json()["summary"]["tables"]
    r = _create(client, run={"cadence_minutes": 7})
    assert r.status_code == 422 and any("cadence" in e for e in r.json()["detail"]["errors"])


# ---- adding a CSV of tables to an existing job --------------------------------------------------

def _job_rows():
    """The job made from CSV above, as its control rows read back."""
    rows = [dict(line["row"], id=i + 1, task_enabled=True) for i, line in enumerate(nj.plan("Engagement extraction", CSV, FAN_IN, 4)["lines"])]
    return rows


def test_tables_added_to_a_job_take_its_settings_and_its_groups():
    more = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
            "sql_a,eng_002,gen,t0003,\"id, x\",id\n"            # a database the job has: its group
            "sql_a,eng_009,gen,t0001,\"id, amount\",id\n"       # a new database on the job's server: one of its groups
            "sql_b,ops_01,gen,t0001,\"id, amount\",id\n"        # a new server: new groups
            "sql_a,eng_001,gen,t0001,\"id, amount\",id\n")      # a table the job already writes: left as it is
    rows = _job_rows()
    plan = nj.plan_for_job("Engagement extraction", more, "ct", rows, 4)
    assert plan["errors"] == []
    by_table = {line["table"]: line for line in plan["lines"]}
    assert by_table["eng_002.gen.t0003"]["group"] == next(r["task_collection"] for r in rows if r["src_database"] == "eng_002")
    assert by_table["eng_009.gen.t0001"]["group"] in {r["task_collection"] for r in rows}
    assert by_table["ops_01.gen.t0001"]["group"].startswith("engagement_extraction_sql_b_c0")
    assert by_table["eng_001.gen.t0001"]["status"] == "in_job"
    assert plan["new_groups"] == [by_table["ops_01.gen.t0001"]["group"]]
    new = by_table["eng_009.gen.t0001"]["row"]
    assert (new["sink_schema"], new["sink_table"], new["client_id"], json.loads(new["client_attrs"])) == (
        "engagements", "t0001", "eng_009", {"engagement": "eng_009"})
    summary = nj.summary(plan)
    assert (summary["tables"], summary["in_job"]) == (3, 1)


def test_a_job_whose_tables_differ_on_a_setting_must_set_it_first():
    rows = _job_rows()
    rows[0]["sink_schema"] = "elsewhere"
    plan = nj.plan_for_job("Engagement extraction", CSV, "ct", rows, 4)
    assert plan["lines"] == [] and "target schema" in plan["errors"][0].replace("sink", "target").replace("_", " ")


def test_new_jobs_name_their_tables_by_unity_catalog_rules():
    lines = nj.plan("Job", NAMED, FAN_IN, 4)["lines"]
    assert [line["row"]["sink_table"] for line in lines] == ["order-lines", "sales_(2024)", "a--b"]


def test_an_existing_job_keeps_the_older_names_when_tables_are_added():
    rows = [dict(line["row"], id=i + 1, task_enabled=True)
            for i, line in enumerate(nj.plan("Engagement extraction", NAMED, FAN_IN, 4, unity_catalog=False)["lines"])]
    more = HEAD + ('sql_a,eng_009,gen,Order-Lines,"id",id\n' + 'sql_a,eng_009,gen,"Sales (2024)","id",id\n'
                   + "sql_a,eng_009,gen,a--b,\"id\",id\n")
    plan = nj.plan_for_job("Engagement extraction", more, "ct", rows, 4, unity_catalog=False)
    assert plan["errors"] == []
    assert [line["row"]["sink_table"] for line in plan["lines"]] == ["order_lines", "sales_2024", "a_b"]


def _older_job_rows():
    """A job made under the older naming rule (no target_names option), as its control rows read back."""
    rule = js.unity_catalog_of({})
    return [dict(line["row"], id=i + 1, task_enabled=True)
            for i, line in enumerate(nj.plan("Engagement extraction", CSV, FAN_IN, 4, unity_catalog=rule)["lines"])]


def test_an_older_job_with_only_safe_names_takes_the_older_rule_when_tables_are_added():
    rows = _older_job_rows()
    assert all(re.fullmatch(r"[a-z0-9_]+", r["sink_table"]) for r in rows)
    more = HEAD + 'sql_a,eng_009,gen,Order-Lines,"id",id\n'
    plan = nj.plan_for_job("Engagement extraction", more, "ct", rows, 4, unity_catalog=js.unity_catalog_of({}))
    assert plan["errors"] == [] and [line["row"]["sink_table"] for line in plan["lines"]] == ["order_lines"]


def test_an_older_job_with_only_safe_names_rediscovers_under_the_older_rule():
    rows = _older_job_rows()
    more = HEAD + 'sql_a,eng_009,gen,Order-Lines,"id",id\n'
    plan = nj.plan_for_job("Engagement extraction", more, "ct", rows, None, set(), js.unity_catalog_of({}))
    assert plan["errors"] == [] and [line["row"]["sink_table"] for line in plan["lines"]] == ["order_lines"]


def test_a_typed_target_name_keeps_its_text_and_is_refused_under_unity_catalog():
    one = HEAD + 'sql_a,eng_001,gen,t0001,"id",id\n'
    refused = nj.plan("Job", one, {**FAN_IN, "sink_table": "Sales 2024"}, 4)
    assert refused["errors"] == [
        "target table 'Sales 2024' has a character Unity Catalog refuses (a space, period, slash or control character): "
        "take it out"]
    kept = nj.plan("Job", one, {**FAN_IN, "sink_table": "Sales 2024"}, 4, unity_catalog=False)
    assert kept["errors"] == [] and kept["lines"][0]["row"]["sink_table"] == "Sales 2024"


def test_two_tables_landing_on_one_target_are_refused_under_the_rule_the_job_uses():
    under_uc = nj.plan("Job", HEAD + 'sql_a,eng_001,gen,Order-Lines,"id",id\nsql_a,eng_001,gen,order_lines,"id",id\n',
                       TABLE_ONLY, 4)
    assert [line["status"] for line in under_uc["lines"]] == ["valid", "valid"]
    same = HEAD + 'sql_a,eng_001,gen,Order-Lines,"id",id\nsql_a,eng_001,gen,order_lines,"id",id\n'
    under_older = nj.plan("Job", same, TABLE_ONLY, 4, unity_catalog=False)
    assert [line["status"] for line in under_older["lines"]] == ["valid", "invalid"]
    dashed = nj.plan("Job", HEAD + 'sql_a,eng_001,gen,Order-Lines,"id",id\nsql_a,eng_001,gen,order-lines,"id",id\n',
                     TABLE_ONLY, 4)
    assert [line["status"] for line in dashed["lines"]] == ["valid", "invalid"]


@pytest.fixture
def job_client(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import main
    import queries as q
    from api_test import FakeCursor, FakeDB

    rows = _job_rows()
    schedule = dict(id=7, name="Engagement extraction", cadence_minutes=60, timezone="UTC", stagger=True, paused=True,
                    options=None, job_variant="default", parameters={}, notify_emails=None)
    members = [dict(id=i, schedule_id=7, task_collection=g, offset_minutes=0, job_id=None, removed_at=None, delete_pending=False)
               for i, g in enumerate(sorted({r["task_collection"] for r in rows}))]

    def answer(sql, params=()):
        if sql == q.schedule_sql():
            return [dict(schedule)]
        if sql == q.schedule_members_sql():
            return [dict(m) for m in members]
        if sql.startswith(q.groups_control_rows_sql().split(" for update")[0][:60]):
            return [dict(r) for r in rows]
        if sql == q.collections_sql():
            return [{"task_collection": m["task_collection"]} for m in members]
        if sql == q.insert_task_sql():
            return [{"id": 1, "made": True}]
        return []

    fake = FakeDB()
    monkeypatch.setattr(main.db, "query", answer)
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): None)
    monkeypatch.setattr(main.db, "fetch", lambda cur, sql, params=(): answer(sql, params))
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield FakeCursor(fake))))
    monkeypatch.setattr(main, "_refuse_job", lambda job, request, action: None)
    synced = []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or [])
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db, c.synced, c.q = fake, synced, q
    return c


MORE = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
        "sql_a,eng_009,gen,t0001,\"id, amount\",id\n"
        "sql_b,ops_01,gen,t0001,\"id, amount\",id\n")


def test_adding_tables_previews_then_writes_them_and_the_new_groups(job_client):
    body = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": MORE}).json()
    assert body["applied"] is False and body["summary"]["tables"] == 2 and job_client.db.statements == []
    body = job_client.post("/api/ingestion-jobs/7/tables", json={"csv": MORE, "dry_run": False}).json()
    assert body["applied"] is True
    inserted = [dict(zip(job_client.q.CONTROL_INSERT_COLUMNS, p)) for p in job_client.db.of("insert into lakefed_ingest.control (")]
    assert [r["src_database"] for r in inserted] == ["eng_009", "ops_01"]
    members = job_client.db.of("insert into lakefed_ingest.schedule_member")
    assert [m[1] for m in members] == body["summary"]["new_groups"] and job_client.synced == [7]


def test_a_line_may_give_its_own_load_type_as_discovery_recommends_it():
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,cursor_column,load_type\n"
            "sql_a,eng_001,gen,t0001,\"id, rv\",id,rv,cursor\n"
            "sql_a,eng_001,gen,t0002,\"id, a\",id,,full\n"
            "sql_a,eng_001,gen,t0003,\"id, a\",id,,\n"
            "sql_a,eng_001,gen,t0004,\"id, a\",id,,cdc\n")
    lines = nj.plan("Job", text, {**FAN_IN, "write_mode_full": "append"}, 4)["lines"]
    assert [(line["row"]["load_type"], line["row"]["write_mode"]) for line in lines[:3]] == [
        ("cursor", "merge"), ("full", "append"), ("ct", "merge")]
    assert lines[0]["row"]["cursor_column"] == "rv" and lines[3]["status"] == "invalid"
    assert "load_type must be one of" in " ".join(lines[3]["errors"])


@pytest.mark.parametrize("gap", ["\n", ",,,,,,,\n", ",,,,,,,full\n", "\n\n,,,,,,,\n"])
def test_a_blank_or_empty_line_shifts_no_table_onto_another_tables_load_type(gap):
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,cursor_column,load_type\n"
            "sql_a,eng_001,gen,t0001,\"id, a\",id,,full\n"
            + gap +
            "sql_a,eng_001,gen,t0002,\"id, rv\",id,rv,cursor\n"
            "sql_a,eng_001,gen,t0003,\"id, a\",id,,\n")
    lines = nj.plan("Job", text, FAN_IN, 4)["lines"]
    assert [(line["row"]["src_table"], line["row"]["load_type"]) for line in lines] == [
        ("t0001", "full"), ("t0002", "cursor"), ("t0003", "ct")]


def test_the_target_is_needed_to_create_but_not_to_preview_the_lines():
    text = HEAD + "sql_a,db1,dbo,t1,\"id, a\",id\n"
    no_target = {**PLAIN, "sink_catalog": "", "sink_schema": ""}
    assert nj.target_errors(nj.settings_of(no_target)) == ["set the target catalog", "set the target schema"]
    assert "set the target catalog" in nj.plan("Job", text, no_target)["errors"]
    previewed = nj.plan("Job", text, no_target, require_target=False)
    assert previewed["errors"] == [] and len(previewed["lines"]) == 1
    assert nj.setting_errors(nj.settings_of(no_target), require_target=False) == []


def test_a_preview_by_lines_needs_no_name_but_its_create_does(client):
    r = _create(client, name="", settings={**FAN_IN, "sink_catalog": ""})
    assert r.status_code == 200 and r.json()["summary"]["tables"] and r.json()["name_missing"] is True
    r = _create(client, dry_run=False, name="", settings={**FAN_IN, "sink_catalog": ""})
    assert r.status_code == 422 and r.json()["detail"]["errors"][:2] == ["name the job", "set the target catalog"]
