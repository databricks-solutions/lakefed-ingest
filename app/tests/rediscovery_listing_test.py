"""What a job's re-discovery reads again from the listing its discovery recorded (rediscovery.specs). Plain function
tests: no database and no app."""
import rediscovery as rd

OLD_AZURE_SETTINGS = {
    "azure_credential": "lab_reader",
    "azure_resource_id": "/subscriptions/0000-1111/resourceGroups/rg-lab/providers/Microsoft.Sql/servers/lab-sql",
}


def test_a_job_made_with_the_old_azure_settings_reads_its_server_again_through_master_without_them():
    whole_server = {"scope": "server", "server": "sql_a", "src_database": None, "tables": None, "filter": "lfc",
                    "source_kind": "discovery", "source_id": "d1"}
    discovery = {"server": "sql_a", "src_connection": "sql_a", "src_host": None, "src_port": None,
                 "src_type": "sqlserver", "secret_key": None, "src_database": "master",
                 "listing": {"pattern": "lfc_%", **OLD_AZURE_SETTINGS}, "finished_at": "2026-10-28T03:00:00+00:00"}
    readings, errors = rd.specs([whole_server], [discovery])
    assert errors == []
    [(server, spec)] = readings
    assert server == "sql_a" and spec["databases"] == "*" and spec["pattern"] == "lfc_%"
    assert not set(OLD_AZURE_SETTINGS) & set(spec)


def _whole_server(source_id="d1"):
    return {"scope": "server", "server": "sql_a", "src_database": None, "tables": None, "filter": "lfc",
            "source_kind": "discovery", "source_id": source_id}


def _discovery(listing, finished_at="2026-10-28T03:00:00+00:00"):
    return {"server": "sql_a", "src_connection": "sql_a", "src_host": None, "src_port": None, "src_type": "sqlserver",
            "secret_key": None, "src_database": "master", "listing": listing, "finished_at": finished_at}


def test_a_server_whose_databases_were_listed_through_azure_is_known_by_its_newest_discovery():
    parts = [_whole_server()]
    assert rd.azure_listed_servers(parts, [_discovery({"pattern": "lfc_%", **OLD_AZURE_SETTINGS})]) == {"sql_a"}
    assert rd.azure_listed_servers(parts, [_discovery({"azure_resource_id": OLD_AZURE_SETTINGS["azure_resource_id"]})]) == {"sql_a"}
    assert rd.azure_listed_servers(parts, [_discovery({"pattern": "lfc_%"})]) == set()
    assert rd.azure_listed_servers(parts, [_discovery(None)]) == set()
    newer_through_master = [_discovery(OLD_AZURE_SETTINGS), _discovery({"pattern": "lfc_%"}, "2026-10-29T03:00:00+00:00")]
    assert rd.azure_listed_servers(parts, newer_through_master) == set()


def test_a_server_taken_by_database_names_is_never_called_azure_listed_because_it_is_read_by_name():
    by_name = {"scope": "database", "server": "sql_a", "src_database": "lfc_eng_001", "tables": None, "filter": None,
               "source_kind": "discovery", "source_id": "d1"}
    assert rd.azure_listed_servers([by_name], [_discovery(OLD_AZURE_SETTINGS)]) == set()


def test_a_failed_read_of_an_azure_listed_server_says_what_to_do_in_plain_words():
    line = rd.read_error("sql_a", "listing its databases ended FAILED: Login failed for user 'lister'.", {"sql_a"})
    assert line.startswith("sql_a: listing its databases ended FAILED: Login failed for user 'lister'. This job's databases")
    for words in ("first listed through Azure", "read from master now", "give it access there", "VIEW ANY DATABASE",
                  "##MS_DefinitionReader##", "type the database names in Add tables", "tables keep running as they are"):
        assert words in line, words
    assert ".." not in line and "\u2014" not in line


def test_a_failed_read_of_any_other_server_is_left_as_it_was():
    assert rd.read_error("sql_b", "its discovery run ended FAILED", {"sql_a"}) == "sql_b: its discovery run ended FAILED"


def test_only_a_master_that_failed_gets_the_note_among_the_databases_of_an_azure_listed_server():
    failed = [{"database": "sql_a/master", "error": "Cannot open database \"master\" requested by the login"},
              {"database": "sql_a/lfc_eng_001", "error": "login failed"},
              {"database": "sql_b/master", "error": "login failed"}]
    lines = rd.database_errors(failed, {"sql_a"})
    assert lines[0].endswith(rd.AZURE_LISTING_NOTE) and lines[0].startswith("sql_a/master: Cannot open database")
    assert lines[1:] == ["sql_a/lfc_eng_001: login failed", "sql_b/master: login failed"]
