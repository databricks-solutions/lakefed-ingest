"""Scale harness tests. The default run is small (40 databases); LAKEFED_SCALE=1 runs the 100,080-table and the
5.3-million-table sizes (see run_scale.py for the table they make). No database and no workspace is touched: everything
runs against the simulated server and the recording fakes."""
import json
import math
import os
import re

import pytest

import paths  # noqa: F401  (sys.path)
import app_steps as apps
import ct_engine
import lakebase_fake
import new_job as nj
import server_catalog as sc
import simulation as sim
import steps

SMALL_DATABASES = 40
SCALE = os.environ.get("LAKEFED_SCALE") == "1"


@pytest.fixture(scope="module")
def small_catalog():
    return sc.ServerCatalog(databases=SMALL_DATABASES)


def test_master_listing_names_every_database_and_refuses_the_unopenable_ones(small_catalog):
    run = steps.discover(small_catalog)
    assert run.listed == small_catalog.database_names()
    assert sorted(run.refused) == sorted(small_catalog.unopenable_names())
    assert run.calls_by_kind["listing"] == 1


def test_each_discovered_database_costs_three_catalog_calls(small_catalog):
    run = steps.discover(small_catalog)
    attempted = run.discovered + len(run.refused)
    assert run.calls_by_kind["tables"] == attempted + len(run.retried)
    assert run.calls_by_kind["columns"] == run.calls_by_kind["pk"] == run.discovered
    assert run.tables == run.discovered * small_catalog.tables_per_database


def test_a_paused_database_is_retried_and_its_waits_are_booked(small_catalog):
    run = steps.discover(small_catalog)
    assert sorted(run.retried) == sorted(small_catalog.paused_names())
    assert run.retry_waits_s > 0


def test_a_name_filter_lists_only_its_family():
    server = sc.ServerCatalog(databases=40)
    run = steps.discover(server, pattern="lfc_eng_%")
    assert run.listed == [n for n in server.database_names() if n.startswith("lfc_eng_")]
    assert all(n.startswith("lfc_eng_") for n in run.order)


def test_azure_listing_pages_and_discovers_the_same_databases(small_catalog):
    run = steps.discover(small_catalog, via_azure=True)
    assert run.calls_by_kind["arm"] == small_catalog.arm_page_count()
    assert run.listed == small_catalog.database_names()


def test_the_found_rows_go_to_the_callback_as_they_are_read(small_catalog):
    seen = []
    run = steps.discover(small_catalog, limit=5, on_found=lambda name, rows: seen.append((name, len(rows))))
    assert [n for n, _ in seen] == [n for n in run.listed if n not in run.refused]
    assert all(count == small_catalog.tables_per_database for _, count in seen)


def test_the_boundary_restores_the_real_reads(small_catalog):
    before = (ct_engine._spark, ct_engine._read)
    steps.discover(small_catalog, limit=2)
    assert (ct_engine._spark, ct_engine._read) == before


def test_wall_time_is_one_worker_at_most_and_the_sum_of_work_at_one_worker(small_catalog):
    run = steps.discover(small_catalog)
    slow = sim.Latency(1.0)
    one_worker = run.wall_s(slow, 1)
    assert one_worker == slow.seconds(run.ledger[sim.MASTER].calls, 0) + sum(
        run.ledger[name].seconds(slow) for name in run.order)
    assert run.wall_s(slow, steps.NOTEBOOK_WORKERS) <= run.wall_s(slow, steps.PREVIOUS_WORKERS) <= one_worker


def test_wall_time_rises_with_latency(small_catalog):
    run = steps.discover(small_catalog)
    assert run.wall_s(sim.Latency(0.25), steps.NOTEBOOK_WORKERS) < run.wall_s(sim.Latency(1.0), steps.NOTEBOOK_WORKERS)


def test_pool_makespan_takes_tasks_in_order_on_the_freest_worker():
    assert sim.pool_makespan([1.0, 1.0, 1.0, 1.0], 2) == 2.0
    assert sim.pool_makespan([3.0, 1.0, 1.0, 1.0], 2) == 3.0
    assert sim.pool_makespan([], 8) == 0.0
    assert sim.pool_makespan([5.0], 32) == 5.0


def test_like_matches_the_patterns_the_harness_uses():
    assert sim.like_matches("lfc_eng_%", "lfc_eng_00012")
    assert not sim.like_matches("lfc_eng_%", "lfc_test_00012")
    assert sim.like_matches("lfc_test_%", "lfc_test_00012")
    assert sim.like_matches("%", "ops_00001")


def test_the_discover_writes_are_a_thousand_tables_to_a_statement_and_a_transaction_per_database(small_catalog):
    run, connection = steps.discover_writes(small_catalog)
    per_database = math.ceil(small_catalog.tables_per_database / steps.TABLE_BATCH)
    assert connection.statements["insert lakefed_ingest.discovered_table"] == run.discovered * per_database
    assert connection.rows["insert lakefed_ingest.discovered_table"] == run.tables
    assert connection.statements["delete lakefed_ingest.discovered_table"] == run.discovered
    assert connection.transactions == run.discovered
    databases = run.discovered + len(run.refused)
    assert connection.statements["insert lakefed_ingest.discovery"] == databases
    assert connection.statements["update lakefed_ingest.discovery"] == databases


def test_the_writer_columns_are_the_notebook_columns():
    with open(os.path.join(paths.ROOT, "src", "lakefed_ingest", "discover_tables.ipynb"), encoding="utf-8") as handle:
        notebook = json.load(handle)
    source = "".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    notebook_cols = re.findall(r'"(\w+)"', re.search(r"TABLE_COLS = \[(.*?)\]", source, re.S).group(1))
    assert notebook_cols == steps.TABLE_COLS


def test_the_notebook_writes_its_batch_size():
    with open(os.path.join(paths.ROOT, "src", "lakefed_ingest", "discover_tables.ipynb"), encoding="utf-8") as handle:
        notebook = json.load(handle)
    source = "".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    assert int(re.search(r"^TABLE_BATCH = (\d+)", source, re.M).group(1)) == steps.TABLE_BATCH


def test_the_recording_connection_counts_executemany_per_row_and_multi_row_values_by_group():
    connection = lakebase_fake.RecordingConnection()
    with connection.cursor() as cursor:
        cursor.executemany("insert into lakefed_ingest.control_change (a) values (%s)", [(1,), (2,), (3,)])
        cursor.execute("insert into lakefed_ingest.control (a, b) values (%s, %s), (%s, %s)", (1, 2, 3, 4))
    assert connection.statements["insert lakefed_ingest.control_change"] == 3
    assert connection.statements["insert lakefed_ingest.control"] == 1
    assert connection.rows["insert lakefed_ingest.control"] == 2


def test_create_by_reference_plans_every_discovered_table_and_writes_it_in_thousands(small_catalog):
    run = apps.create_by_reference(small_catalog)
    assert run.errors == []
    assert run.tables == steps.discover(small_catalog).tables
    assert run.connection.statements["insert lakefed_ingest.control"] == math.ceil(run.tables / 1000)
    change_log_statements = run.connection.statements["insert lakefed_ingest.control_change"]
    assert change_log_statements == math.ceil(run.tables / 1000) + 1   # plus one for the groups' schedule_create lines
    assert run.connection.statements["insert lakefed_ingest.schedule_member"] == run.groups
    assert run.connection.statements["insert lakefed_ingest.schedule"] == 1


def test_create_by_database_checks_every_database_then_writes_each_in_its_own_transaction(small_catalog):
    run = apps.create_by_database(small_catalog)
    databases = small_catalog.databases - len(small_catalog.unopenable_names())
    assert run.errors == []
    assert run.databases == databases and run.tables == steps.discover(small_catalog).tables
    assert run.connection.transactions == databases + 2      # plus the job's own, and the task's result
    assert run.connection.statements["insert lakefed_ingest.control"] == databases
    assert run.connection.statements["insert lakefed_ingest.schedule_member"] == databases
    assert run.connection.statements["update lakefed_ingest.app_task"] == databases + 2


def test_create_by_chunk_writes_the_same_tables_in_packed_transactions_and_fewer_statements(small_catalog):
    before = apps.create_by_database(small_catalog)
    after = apps.create_by_chunk(small_catalog)
    assert after.errors == [] and after.databases == before.databases and after.tables == before.tables
    assert after.groups == before.groups
    assert after.connection.transactions < before.connection.transactions
    assert after.connection.reads() < before.connection.reads()
    assert after.connection.total_statements() < before.connection.total_statements()
    assert after.connection.round_trips() < before.connection.round_trips()
    assert after.connection.statements["insert lakefed_ingest.schedule_member"] <= after.connection.transactions


def test_an_upload_writes_its_header_and_each_line_a_thousand_to_a_statement(small_catalog):
    run = apps.upload_by_reference(small_catalog)
    assert run.errors == []
    assert run.lines == steps.discover(small_catalog).tables
    assert run.connection.statements["insert lakefed_ingest.table_upload"] == 1
    assert run.connection.statements["insert lakefed_ingest.table_upload_line"] == math.ceil(run.lines / 1000)


def test_the_line_caps_refuse_the_file_past_them():
    errors, _ = apps.create_refusal(nj.MAX_LINES + 1)
    assert errors == [f"more than {nj.MAX_LINES:,} lines: at most {nj.MAX_LINES:,} in one import"]
    errors, _ = apps.upload_refusal(nj.MAX_LINES + 1)
    assert errors == [f"more than {nj.MAX_LINES:,} lines: at most {nj.MAX_LINES:,} in one file"]


def test_the_collections_page_reads_the_control_table_once():
    shapes = {s["path"]: s for s in apps.monitoring_shapes(("/api/collections",))}
    assert shapes["/api/collections"]["status"] == 200
    assert shapes["/api/collections"]["kinds"] == {"select lakefed_ingest.control": 1}


def test_no_connection_is_made_while_the_steps_run():
    import lakebase
    with lakebase_fake.no_connections():
        with pytest.raises(RuntimeError):
            lakebase.connect()
        with pytest.raises(RuntimeError):
            lakebase.workspace()


@pytest.mark.skipif(not SCALE, reason="LAKEFED_SCALE=1 runs the 100,000-table size")
def test_create_and_upload_at_a_hundred_thousand_tables_are_planned_in_full():
    catalog = sc.ServerCatalog(databases=417)
    run = apps.create_by_database(catalog)
    assert run.errors == [] and run.tables == (417 - len(catalog.unopenable_names())) * 240
    upload = apps.upload_by_reference(catalog)
    assert upload.lines == run.tables and upload.errors == []


@pytest.mark.skipif(not SCALE, reason="LAKEFED_SCALE=1 runs the 100,000-table size")
def test_a_hundred_thousand_tables_discover_in_one_run():
    server = sc.ServerCatalog(databases=417)
    run = steps.discover(server)
    assert run.tables == (417 - len(run.refused)) * 240
    assert len(run.listed) == 417


@pytest.mark.skipif(not SCALE, reason="LAKEFED_SCALE=1 runs the 22,000-database size (several minutes)")
def test_twenty_two_thousand_databases_discover_every_one_the_server_can_open():
    catalog = sc.ServerCatalog(databases=22_000)
    run = steps.discover(catalog)
    assert sorted(run.refused) == sorted(catalog.unopenable_names())
    assert run.discovered == 22_000 - len(catalog.unopenable_names())
    assert run.tables == run.discovered * sc.DEFAULT_TABLES_PER_DATABASE
