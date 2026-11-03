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
