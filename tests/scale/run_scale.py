"""Runs the scale steps and prints the tables docs/scale.md reports. Nothing connects to a database or a workspace.

    python tests/scale/run_scale.py                    # 40 databases (the default run's size)
    LAKEFED_SCALE=1 python tests/scale/run_scale.py    # 417 databases (100,080 tables) and 22,000 (5,280,000 tables)
    python tests/scale/run_scale.py 417 22000          # the sizes given (run one per process for clean peak memory)

Discovery runs in full at each size. The create takes the route the app takes at that size: in the request when the
selection is at most BACKGROUND_ROWS tables (measured at the largest such size), and database by database in the
background above it (measured in full at each size). The in-request planner's own refusal at the line cap is measured
by reading the cap's worth of lines. Upload runs in full where read_upload takes the size, and as a refusal probe above
the line cap. Server times are simulated from the latency given (see simulation.Latency).
"""
from __future__ import annotations

import os
import sys
import time
from typing import List, Optional

import paths  # noqa: F401  (sys.path)
import app_steps as apps
import new_job as nj
import server_catalog
import simulation as sim
import steps

LATENCIES_S = (0.25, 0.5, 0.9, 1.5, 2.0)
WORKER_COUNTS = (steps.PREVIOUS_WORKERS, steps.NOTEBOOK_WORKERS)
TABLES_PER_DATABASE = server_catalog.DEFAULT_TABLES_PER_DATABASE
DEFAULT_SIZES = (40,)
ROUND_TRIP_S = 0.002
SCALE_SIZES = (417, 22_000)


def sizes_from_environment() -> tuple:
    return SCALE_SIZES if os.environ.get("LAKEFED_SCALE") == "1" else DEFAULT_SIZES


def peak_memory_mb() -> float:
    """The process's peak resident memory so far, in megabytes (macOS reports bytes, Linux kilobytes)."""
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def discovery_result(databases: int) -> dict:
    """Discovery of a server of that many databases, with its Lakebase writes: calls, statements, rows and the simulated
    wall time at each latency and worker count."""
    catalog = server_catalog.ServerCatalog(databases=databases)
    run, connection = steps.discover_writes(catalog)
    wall = {(latency, workers): run.wall_s(sim.Latency(latency), workers)
            for latency in LATENCIES_S for workers in WORKER_COUNTS}
    return {"databases": databases, "tables": run.tables, "discovered": run.discovered,
            "refused": len(run.refused), "retried": len(run.retried), "calls": run.calls(),
            "calls_by_kind": dict(run.calls_by_kind), "statements": connection.total_statements(),
            "rows": connection.total_rows(), "python_s": run.cpu_s, "peak_mb": peak_memory_mb(), "wall": wall,
            "retry_waits_s": run.retry_waits_s}


def in_request_create_result() -> dict:
    """The create the route runs in the request: by reference, for the largest selection it takes in the request (the
    BACKGROUND_ROWS count, which is 20 databases of 240 tables), measured in full. Its planner's refusal at the line cap
    is measured too: the route never plans a bigger selection (the count sends it to the background first), so the
    refusal is the planner's own limit, read to the cap's line."""
    databases = apps.app.BACKGROUND_ROWS // TABLES_PER_DATABASE
    run = apps.create_by_reference(server_catalog.ServerCatalog(databases=databases))
    errors, probe_s = apps.create_refusal(nj.MAX_LINES + 1)
    return {"databases": databases, "tables": run.tables, "errors": run.errors, "groups": run.groups,
            "csv_bytes": run.csv_bytes, "csv_s": run.csv_s, "plan_s": run.plan_s, "write_s": run.write_s,
            "statements": run.connection.total_statements(), "rows": run.connection.total_rows(),
            "peak_mb": peak_memory_mb(), "probe_lines": nj.MAX_LINES + 1, "probe_errors": errors, "probe_s": probe_s}


def create_by_database_result(databases: int, step=apps.create_by_database) -> dict:
    """The create the background task runs for a selection above BACKGROUND_ROWS: database by database, a check pass
    then the writes, measured in full at this size. ``step`` is the path measured: create_by_database as it ran
    before the writes were packed, create_by_chunk as the route runs it now."""
    catalog = server_catalog.ServerCatalog(databases=databases)
    run = step(catalog)
    return {"databases": databases, "tables": run.tables, "groups": len(run.groups), "errors": run.errors,
            "check_s": run.check_s, "write_s": run.write_s, "read_s": run.read_s,
            "statements": run.connection.total_statements(), "rows": run.connection.total_rows(),
            "reads": run.connection.reads(), "transactions": run.connection.transactions,
            "round_trips": run.connection.round_trips(), "peak_mb": peak_memory_mb()}


def upload_result(databases: int) -> dict:
    """An upload of a server's tables as one CSV: the full upload when read_upload takes the size, or its refusal probe
    at the cap, with the body size the route would see."""
    catalog = server_catalog.ServerCatalog(databases=databases)
    tables = databases * TABLES_PER_DATABASE
    if tables <= nj.MAX_LINES:
        run = apps.upload_by_reference(catalog)
        return {"databases": databases, "tables": tables, "measured": True, "errors": run.errors, "lines": run.lines,
                "csv_bytes": run.csv_bytes, "read_s": run.read_s, "write_s": run.write_s,
                "statements": run.connection.total_statements() if run.connection else 0,
                "rows": run.connection.total_rows() if run.connection else 0}
    errors, probe_s = apps.upload_refusal(nj.MAX_LINES + 1)
    line_bytes = apps.sample_line_bytes()
    return {"databases": databases, "tables": tables, "measured": False, "errors": errors,
            "probe_lines": nj.MAX_LINES + 1, "probe_s": probe_s, "line_bytes": line_bytes,
            "one_body_bytes": line_bytes * tables, "uploads_at_cap": -(-tables // nj.MAX_LINES),
            "capped_body_bytes": line_bytes * nj.MAX_LINES, "body_limit_bytes": apps.app.MAX_UPLOAD_BYTES}


def _seconds(value: float) -> str:
    return f"{value:,.1f}"


def print_discovery(results: List[dict]) -> None:
    print("## Discovery (discover_tables.ipynb, with its Lakebase writes)")
    print()
    print("| Databases | Tables | Refused | Retried | Calls | Statements | Rows | Python s | "
          "Peak MB (process so far) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['databases']:,} | {r['tables']:,} | {r['refused']} | {r['retried']} | {r['calls']:,} | "
              f"{r['statements']:,} | {r['rows']:,} | {_seconds(r['python_s'])} | {r['peak_mb']:,.0f} |")
    print()
    print("Simulated wall time (s), discovery's listing plus the databases on a pool, by per-call latency:")
    print()
    print("| Databases | Workers | " + " | ".join(f"{l} s" for l in LATENCIES_S) + " |")
    print("|---|---|" + "---|" * len(LATENCIES_S))
    for r in results:
        for workers in WORKER_COUNTS:
            cells = " | ".join(_seconds(r["wall"][(l, workers)]) for l in LATENCIES_S)
            print(f"| {r['databases']:,} | {workers} | {cells} |")
    print()


def print_create(in_request: dict, before: List[dict], after: List[dict]) -> None:
    print("## Create in the request (new_job.plan and the writes of create_ingestion_job, the route under the count)")
    print()
    print(f"- {in_request['databases']:,} databases, {in_request['tables']:,} tables: planned in "
          f"{_seconds(in_request['plan_s'])} s (CSV {_seconds(in_request['csv_s'])} s, writes "
          f"{_seconds(in_request['write_s'])} s), {in_request['statements']:,} statements, "
          f"{in_request['rows']:,} rows, {in_request['groups']} groups, {in_request['csv_bytes']:,} CSV bytes, errors "
          f"{in_request['errors'] or 'none'}, peak {in_request['peak_mb']:,.0f} MB")
    print(f"- Planner's refusal at the line cap, measured: {in_request['probe_lines']:,} lines read in "
          f"{_seconds(in_request['probe_s'])} s: {in_request['probe_errors']}")
    print()
    print("## Create in the background, database by database (a check pass, then the writes)")
    print()
    for label, results in (("before the packing", before), ("after the packing", after)):
        for r in results:
            print(f"- {label}: {r['databases']:,} databases, {r['tables']:,} tables: checked in "
                  f"{_seconds(r['check_s'])} s, written in {_seconds(r['write_s'])} s (the reads of each database's "
                  f"rows, "
                  f"{_seconds(r['read_s'])} s, are in neither), {r['groups']} groups, {r['statements']:,} statements, "
                  f"{r['reads']:,} of them reads, {r['rows']:,} rows, {r['transactions']:,} transactions, "
                  f"{r['round_trips']:,} round trips (about {r['round_trips'] * ROUND_TRIP_S:,.1f} s at 2 ms each), "
                  f"errors {r['errors'] or 'none'}, peak {r['peak_mb']:,.0f} MB")
    print()


def print_upload(results: List[dict]) -> None:
    print("## Upload of a CSV of tables (read_upload and the line writes of upload_tables)")
    print()
    for r in results:
        if r["measured"]:
            print(f"- {r['databases']:,} databases, {r['lines']:,} lines: read in {_seconds(r['read_s'])} s, "
                  f"written in {_seconds(r['write_s'])} s, {r['statements']:,} statements, {r['rows']:,} rows, "
                  f"{r['csv_bytes']:,} CSV bytes")
        else:
            one_mb = r["one_body_bytes"] / 1e6
            cap_mb = r["capped_body_bytes"] / 1e6
            print(f"- {r['databases']:,} databases, {r['tables']:,} tables: refused at the cap "
                  f"({r['probe_lines']:,} lines probe, {_seconds(r['probe_s'])} s): {r['errors']}. "
                  f"At about {r['line_bytes']:.0f} bytes a line, one upload would be about {one_mb:,.0f} MB against "
                  f"the {r['body_limit_bytes']:,} byte body limit; at the cap, {r['uploads_at_cap']} uploads of "
                  f"about {cap_mb:,.1f} MB each.")
    print()


def print_monitoring(shapes: List[dict]) -> None:
    print("## Monitoring pages (the statements each page issues; empty answers, so the shape and not the row count)")
    print()
    print("| Page | Status | Statements | Statement kinds |")
    print("|---|---|---|---|")
    for shape in shapes:
        kinds = ", ".join(f"{kind} x{count}" for kind, count in sorted(shape["kinds"].items())) or "none"
        print(f"| {shape['path']} | {shape['status']} | {shape['statements']} | {kinds} |")
    print()


def main(argv: Optional[List[str]] = None) -> None:
    sizes = tuple(int(a) for a in (argv or []) if a.isdigit()) or sizes_from_environment()
    started = time.perf_counter()
    print(f"Sizes (databases): {', '.join(f'{s:,}' for s in sizes)}")
    print()
    print_discovery([discovery_result(s) for s in sizes])
    print_create(in_request_create_result(), [create_by_database_result(s) for s in sizes],
                 [create_by_database_result(s, apps.create_by_chunk) for s in sizes])
    print_upload([upload_result(s) for s in sizes])
    print_monitoring(apps.monitoring_shapes())
    print(f"Total Python time: {time.perf_counter() - started:,.0f} s")


if __name__ == "__main__":
    main(sys.argv[1:])
