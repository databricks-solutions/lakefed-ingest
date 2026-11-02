"""The roll-ups of per-database rows (rollup.py) into groups, servers and connections."""
import rollup


def state(server, group, database, tables=0, current=0, late=0, first=0, failing=0, no_sla=0, connection=None,
          host=None, newest=None, oldest=None):
    """A database's rollup row: its loaded tables (current and late), and whether it is late (a late table)."""
    return dict(server=server, task_collection=group, src_database=database, tables=tables, state_loaded=current + late,
                late_database=late > 0, state_first_load=first, state_failing=failing, no_sla_tables=no_sla,
                src_connection=connection, src_host=host, newest_success_at=newest, oldest_success_at=oldest)


STATES = [state("srvX", "c1", "Sales", tables=3, current=3, newest="2026-10-26T10:00"),
          state("srvY", "c1", "Sales", tables=2, current=1, late=1, newest="2026-10-26T11:00"),
          state("srvY", "c1", "Archive", tables=0),
          state("srvY", "c2", "Hr", tables=4, first=2, failing=2, no_sla=4, connection="hr_conn")]


def test_two_servers_same_named_databases_are_two_and_disabled_only_ones_none():
    g = rollup.groups(STATES)["c1"]
    assert (g["databases"], g["servers"], g["server"]) == (2, 2, "srvX")
    assert (g["enabled_tables"], g["state_loaded"], g["late_databases"]) == (5, 5, 1)
    assert g["newest_success_at"] == "2026-10-26T11:00"


def test_state_sums_equal_the_inputs():
    groups = rollup.groups(STATES)
    for k in rollup.STATES + ("no_sla_tables",):
        assert sum(g[k] for g in groups.values()) == sum(s[k] for s in STATES)


def test_a_group_whose_tables_are_all_disabled_is_still_listed():
    g = rollup.groups([state("srvZ", "c9", "Old", tables=0)])["c9"]
    assert (g["enabled_tables"], g["databases"], g["server"]) == (0, 0, None)


def test_servers_roll_up_databases_groups_and_running_groups():
    by = {s["server"]: s for s in rollup.servers(STATES, running_groups={"c2"})}
    assert (by["srvY"]["databases"], by["srvY"]["tables"], by["srvY"]["collections"]) == (2, 6, 2)
    assert (by["srvY"]["running_collections"], by["srvX"]["running_collections"]) == (1, 0)
    assert [s["server"] for s in rollup.servers(STATES)] == ["srvX", "srvY"]


def test_a_connection_matches_by_name_or_host_case_insensitively():
    rows = STATES + [state("sql-w.example.com:1433", "c3", "Ops", tables=1, host="sql-w.example.com")]   # a JDBC server: host:port
    assert [d["src_database"] for d in rollup.connection_databases(rows, "hr_conn", None)] == ["Hr"]
    assert [d["src_database"] for d in rollup.connection_databases(rows, "other", "SQL-W.example.com")] == ["Ops"]
    assert rollup.tracked(rows, "hr_conn", "") == {"databases": 1, "tables": 4, "collections": 1}
    assert rollup.connection_databases(rows, "nothing", "") == []


SCHEDULES = [dict(id=1, name="East hourly", cadence_minutes=60, timezone="UTC", paused=False),
             dict(id=2, name="Nightly", cadence_minutes=1440, timezone="UTC", paused=True)]
MEMBERS = [dict(schedule_id=1, task_collection="c1", offset_minutes=0, removed_at=None),
           dict(schedule_id=2, task_collection="c1", offset_minutes=0, removed_at=None),
           dict(schedule_id=2, task_collection="c2", offset_minutes=30, removed_at="2026-10-01T00:00:00+00:00"),
           dict(schedule_id=2, task_collection="c3", offset_minutes=10, removed_at=None)]


def test_a_group_in_two_jobs_adds_to_both_and_one_in_no_job_is_not_in_any_job():
    states = STATES + [state("srvZ", "c3", "Ops", tables=5, current=5)]
    jobs, not_in_job = rollup.jobs(states, SCHEDULES, MEMBERS)
    by_name = {j["name"]: j for j in jobs}
    assert (by_name["East hourly"]["enabled_tables"], by_name["East hourly"]["databases"]) == (5, 2)
    assert (by_name["Nightly"]["enabled_tables"], by_name["Nightly"]["databases"], by_name["Nightly"]["servers"]) == (10, 3, 3)
    assert by_name["Nightly"]["groups"] == ["c1", "c3"] and by_name["Nightly"]["late_databases"] == 1
    assert not_in_job["groups"] == ["c2"] and (not_in_job["enabled_tables"], not_in_job["state_failing"]) == (4, 2)
    assert rollup.jobs(STATES, SCHEDULES, MEMBERS + [dict(schedule_id=1, task_collection="c2", offset_minutes=30,
                                                          removed_at=None)])[1] is None


def test_sources_list_instances_with_their_jobs_and_a_connection_with_nothing_registered():
    states = [state("srvA", "c1", "Sales", tables=3, current=3, connection="sql_a"),
              state("sql-w.example.com:1433", "c3", "Ops", tables=5, current=4, failing=1, host="sql-w.example.com"),
              state("sql-w.example.com:1433", "c1", "Old", tables=0, host="sql-w.example.com")]     # c1 has only disabled rows here
    connections = [dict(name="sql_a", connection_type="SQLSERVER", host="a.example.com", port="1433"),
                   dict(name="pg_empty", connection_type="POSTGRESQL", host="pg.example.com", port="5432")]
    not_ingested = [dict(endpoint="sql-w.example.com:1433", src_host="SQL-W.example.com", src_database="Ops", discovered=9, not_registered=4,
                         largest_not_registered="dbo.big", discovered_at="2026-10-26T10:00:00+00:00")]
    JDBC = "sql-w.example.com:1433"     # a JDBC server: its host and port
    found = rollup.sources(states, connections, not_ingested, SCHEDULES, MEMBERS)
    by_name = {i["server"]: i for i in found["instances"]}
    assert by_name["srvA"]["read_via"] == [{"kind": "uc", "name": "sql_a", "connection_type": "SQLSERVER", "host": "a.example.com"}]
    assert by_name[JDBC]["read_via"] == [{"kind": "jdbc", "host": "sql-w.example.com"}]
    assert by_name[JDBC]["not_registered"] == 4 and by_name[JDBC]["not_ingested"][0]["largest_not_registered"] == "dbo.big"
    assert [j["name"] for j in by_name[JDBC]["jobs"]] == ["Nightly"] and by_name[JDBC]["state_failing"] == 1
    nightly = by_name[JDBC]["jobs"][0]
    assert nightly["groups"] == ["c1", "c3"] and len(nightly["groups"]) == by_name[JDBC]["groups"]     # as the instance counts
    assert [j["groups"] for j in by_name["srvA"]["jobs"]] == [["c1"], ["c1"]]       # not all of Nightly's (c3 is on srvB)
    assert [j["name"] for j in by_name["srvA"]["jobs"]] == ["East hourly", "Nightly"]
    assert by_name["pg_empty"]["nothing_registered"] is True and by_name["pg_empty"]["tables"] == 0
    assert (found["ingested"], found["nothing_registered"], found["not_registered"]) == (2, 1, 4)


def test_an_instance_says_where_its_rows_labels_differ_and_when_the_app_cannot_see_its_host():
    # One UC connection the app cannot list, its rows labelled two ways: one instance, its labels named, its host
    # marked as not visible rather than left out.
    states = [dict(state("lfc_sql", "c1", "lfc_test", tables=225, connection="lfc_sql"), instance_label="lab-sql-b"),
              dict(state("lfc_sql", "c2", "lfc_test", tables=775, connection="lfc_sql"), instance_label="LFC_SQL"),
              dict(state("lfc_sql", "c2", "lfc_test", tables=0, connection="lfc_sql"), instance_label="disabled-only")]
    found = rollup.sources(states, [], [], SCHEDULES, MEMBERS)["instances"]
    assert [(i["server"], i["databases"], i["tables"]) for i in found] == [("lfc_sql", 1, 1000)]
    assert found[0]["labels"] == [{"label": "LFC_SQL", "tables": 775}, {"label": "lab-sql-b", "tables": 225}]
    assert found[0]["name"] == "lfc_sql"
    assert found[0]["read_via"] == [{"kind": "uc", "name": "lfc_sql", "connection_type": None, "host": None,
                                     "host_hidden": True}]



def test_one_label_on_all_of_a_servers_tables_is_its_name_and_no_label_counts_as_one():
    one = [dict(state("sql-1.lab:1433", "c1", "db1", tables=10, host="sql-1.lab"), instance_label="sql-lab-1")]
    found = rollup.sources(one, [], [], SCHEDULES, MEMBERS)["instances"][0]
    assert (found["name"], found["labels"]) == ("sql-lab-1", [])
    mixed = one + [dict(state("sql-1.lab:1433", "c2", "db2", tables=4, host="sql-1.lab"), instance_label=None)]
    found = rollup.sources(mixed, [], [], SCHEDULES, MEMBERS)["instances"][0]
    assert (found["name"], found["labels"]) == ("sql-1.lab:1433", [{"label": None, "tables": 4},
                                                                   {"label": "sql-lab-1", "tables": 10}])


def test_two_ports_on_one_host_are_two_instances_and_a_discovery_goes_to_its_own():
    states = [state("db.example:1433", "c1", "Sales", tables=3, host="db.example"),
              state("db.example:1500", "c2", "Sales", tables=2, host="db.example")]
    found_tables = [dict(endpoint="db.example:1500", src_host="db.example", src_database="Sales", discovered=5,
                         not_registered=2, largest_not_registered="dbo.t", discovered_at="2026-10-26T10:00:00+00:00")]
    connections = [dict(name="other_port", connection_type="SQLSERVER", host="db.example", port="1600")]
    found = rollup.sources(states, connections, found_tables, SCHEDULES, MEMBERS)
    by_name = {i["server"]: i for i in found["instances"]}
    assert (by_name["db.example:1433"]["not_registered"], by_name["db.example:1500"]["not_registered"]) == (0, 2)
    assert found["not_registered"] == 2 and by_name["other_port"]["nothing_registered"] is True


def test_a_connection_is_matched_by_name_whatever_its_case():
    states = [state("conn_a", "c1", "Sales", tables=3, connection="Conn_A")]
    connections = [dict(name="conn_a", connection_type="SQLSERVER", host="a.example", port="1433")]
    found = rollup.sources(states, connections, [], SCHEDULES, MEMBERS)["instances"]
    assert [(i["server"], i["read_via"][0]["host"]) for i in found] == [("conn_a", "a.example")]
    assert [d["src_database"] for d in rollup.connection_databases(states, "conn_a", "a.example")] == ["Sales"]


def test_a_connection_claims_rows_at_its_own_port_and_a_stale_host_on_a_connection_row_is_not_a_jdbc_path():
    states = [state("db.example:1433", "c1", "A", tables=2, host="db.example"),
              state("db.example:1500", "c2", "B", tables=3, host="db.example"),
              state("conn_a", "c3", "C", tables=4, connection="conn_a", host="old.example")]   # reads through conn_a
    assert [d["src_database"] for d in rollup.connection_databases(states, "on_1500", "db.example", "1500", "SQLSERVER")] == ["B"]
    assert [d["src_database"] for d in rollup.connection_databases(states, "on_default", "db.example", None, "SQLSERVER")] == ["A"]
    conn = {i["server"]: i for i in rollup.sources(states, [], [], SCHEDULES, MEMBERS)["instances"]}["conn_a"]
    assert [v["kind"] for v in conn["read_via"]] == ["uc"]


def test_a_late_database_counts_once_however_many_grains_it_has():
    rows = [state("srvY", "c1", "Sales", tables=2, current=1, late=1),
            state("srvY", "c1", "Sales", tables=1, late=1, connection="other")]
    g = rollup.groups(rows)["c1"]
    assert (g["late_databases"], g["state_loaded"]) == (1, 3)
    assert [s["late_databases"] for s in rollup.servers(rows)] == [1]


def test_a_scanned_database_is_loaded_with_its_late_tables_and_is_late_when_one_is():
    row = rollup.from_scan(dict(server="s", task_collection="c", src_database="d", tables=4, state_current=2,
                                state_late=2, state_first_load=0, state_failing=0))
    assert (row["state_loaded"], row["late_database"]) == (4, True)
    assert "state_current" not in row and "state_late" not in row
