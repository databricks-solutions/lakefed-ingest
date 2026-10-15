#!/usr/bin/env python3
"""
db2_datagen.py — DB2 test data generator for lakefed-ingest ingestion testing.

Creates and seeds three tables that cover each ingestion scenario:

  Scenario              Table            Rows (default)  load_type        load_partitioned
  ─────────────────────────────────────────────────────────────────────────────────────────
  Full load             test_reference   1 K             full             false
  Incremental watermark test_orders      50 K seed       incremental      false
  Partitioned full load test_events      800 K           full             true

  Note: IBM DB2 on Cloud Lite has a ~200 MB storage cap. 800K event rows
  (~175 MB with indexes) is the safe ceiling for that plan.

Registering test tasks in the control table (run in a Databricks SQL Warehouse notebook):

  -- Step 1: Preview values — validate before inserting
  CREATE OR REPLACE TEMP VIEW control_src AS
  SELECT
    col1  AS job_name,
    col2  AS task_collection,
    col3  AS src_type,
    col4  AS src_connection,
    col5  AS src_database,
    col6  AS src_catalog,
    col7  AS src_schema,
    col8  AS src_table,
    col9  AS sink_catalog,
    col10 AS sink_schema,
    col11 AS sink_table,
    col12 AS enable_iceberg_reads,
    col13 AS primary_key,
    col14 AS sink_cluster_cols,
    col15 AS load_type,
    col16 AS load_partitioned,
    col17 AS select_list,
    col18 AS watermark_col_name,
    col19 AS watermark_col_type,
    col20 AS watermark_col_start_value,
    col21 AS partition_col,
    col22 AS partition_size_mb,
    col23 AS use_remote_query,
    col24 AS task_enabled
  FROM (VALUES
    -- job_name         task_collection  src_type   src_connection          src_database  src_catalog  src_schema  src_table         sink_catalog      sink_schema  sink_table        iceberg  primary_key         cluster_cols  load_type      partitioned  select  wm_col       wm_type      wm_start                  part_col    part_mb  remote  enabled
    ('lakefed_ingest', 'db2_load_test', 'db2_luw', 'db2_cloud_connection', 'BLUDB',      NULL,        'hft64286', 'test_reference', 'lakefed_ingest', 'db2_test', 'test_reference', false,   ARRAY(),            ARRAY(),      'full',        false,       '*',    NULL,        NULL,        NULL,                     NULL,       NULL,    true,   true),
    ('lakefed_ingest', 'db2_load_test', 'db2_luw', 'db2_cloud_connection', 'BLUDB',      NULL,        'hft64286', 'test_orders',    'lakefed_ingest', 'db2_test', 'test_orders',    false,   ARRAY('order_id'),  ARRAY(),      'incremental', false,       '*',    'updated_at','timestamp', '1000-01-01 00:00:00.000',    NULL,       NULL,    true,   true),
    ('lakefed_ingest', 'db2_load_test', 'db2_luw', 'db2_cloud_connection', 'BLUDB',      NULL,        'hft64286', 'test_events',    'lakefed_ingest', 'db2_test', 'test_events',    false,   ARRAY(),            ARRAY(),      'full',        true,        '*',    NULL,        NULL,        NULL,                     'event_id', 512,     true,   true)
  );

  SELECT * FROM control_src;

  -- Step 2: Merge into control table (safe to re-run — matched on src_type + src_schema + src_table)
  MERGE WITH SCHEMA EVOLUTION INTO lakefed_ingest.default.control AS t
  USING control_src AS s
  ON  t.src_type   = s.src_type
  AND t.src_schema = s.src_schema
  AND t.src_table  = s.src_table
  WHEN MATCHED     THEN UPDATE SET *
  WHEN NOT MATCHED THEN INSERT *;

Requirements:
    pip install ibm_db

Credentials are read from scripts/.env (see .env.example for format).
CLI flags override .env values when both are present.

Usage:
    # Use credentials from scripts/.env (recommended)
    python scripts/db2_datagen.py [--action create|seed|generate|all|drop|count]

    # Override specific values
    python scripts/db2_datagen.py --schema MYSCHEMA --action count

    # Fully explicit (no .env needed)
    python scripts/db2_datagen.py \\
        --host <host> --port 50000 --database <db> \\
        --user <user> --password <pw> --schema <schema> \\
        [--action create|seed|generate|all|drop|count] \\
        [--events-rows 5000000] [--orders-rows 50000]

Actions:
    create    — DDL only (create tables + indexes)
    seed      — insert initial rows into all three tables
    generate  — run incremental data generator (inserts + updates on test_orders)
    all       — create + seed + generate  (default)
    drop      — drop all three tables
    count     — print row counts for all three tables
"""

import argparse
import datetime
import json
import random
import string
import sys
import time
import uuid
from pathlib import Path

try:
    import ibm_db_dbi as db2
except ImportError:
    sys.exit(
        "ibm_db is not installed.\n"
        "Install it with:  pip install ibm_db\n"
        "See: https://github.com/ibmdb/python-ibmdb"
    )

# Default .env path — relative to this script's directory
_ENV_PATH = Path(__file__).parent / ".env"


# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

def load_env(path: Path = _ENV_PATH) -> dict:
    """
    Parse scripts/.env.  Accepts the IBM Cloud credential format:
        "hostname": "..."
        "port": "..."
        "database": "..."
        "schema_name": "..."
        "username": "..."
        "password": "..."
        "ssl": "true"

    Also accepts a standard JSON object (with surrounding braces).
    Returns a plain dict with string values; empty dict if file not found.
    """
    if not path.exists():
        return {}
    text = path.read_text().strip()
    # Already a JSON object?
    if text.startswith("{"):
        return json.loads(text)
    # Bare key-value lines — strip trailing commas, wrap in braces
    lines = [ln.strip().rstrip(",") for ln in text.splitlines() if ln.strip()]
    return json.loads("{\n" + ",\n".join(lines) + "\n}")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(host: str, port: int, database: str, user: str, password: str, ssl: bool = False):
    dsn = (
        f"DATABASE={database};HOSTNAME={host};PORT={port};"
        f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        "CONNECTTIMEOUT=30;"
    )
    if ssl:
        dsn += "Security=SSL;"
    conn = db2.connect(dsn, "", "")
    conn.set_autocommit(False)
    return conn


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL: dict[str, str] = {
    # ── Scenario 1: Full load ──────────────────────────────────────────────
    # Small static reference / lookup table. Re-ingested in full each run.
    # No watermark column needed.
    "test_reference": """
        CREATE TABLE {schema}.test_reference (
            ref_id          INTEGER         NOT NULL PRIMARY KEY,
            ref_code        VARCHAR(10)     NOT NULL,
            ref_description VARCHAR(255),
            category        VARCHAR(50)     NOT NULL,
            ref_value       DECIMAL(10, 2)  NOT NULL,
            is_active       SMALLINT        NOT NULL DEFAULT 1,
            created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT TIMESTAMP
        )
    """,

    # ── Scenario 2: Incremental with watermark ─────────────────────────────
    # Orders table with updated_at timestamp as watermark.
    # Primary key: order_id  — required for MERGE in copy_data_incremental.
    # Rows are both inserted (new orders) and updated (status changes).
    "test_orders": """
        CREATE TABLE {schema}.test_orders (
            order_id    INTEGER         NOT NULL GENERATED ALWAYS AS IDENTITY
                        (START WITH 1 INCREMENT BY 1) PRIMARY KEY,
            customer_id INTEGER         NOT NULL,
            product_id  INTEGER         NOT NULL,
            quantity    INTEGER         NOT NULL,
            unit_price  DECIMAL(10, 2)  NOT NULL,
            total_price DECIMAL(12, 2)  NOT NULL,
            status      VARCHAR(20)     NOT NULL,
            region      VARCHAR(30)     NOT NULL,
            updated_at  TIMESTAMP       NOT NULL DEFAULT CURRENT TIMESTAMP
        )
    """,

    # ── Scenario 3: Partitioned full load ──────────────────────────────────
    # Large append-only event log (~5M rows).  event_id BIGINT is used as the
    # partition column (numeric range → stride-based partitioning via UDTF).
    # No watermark; full overwrite each run, split across N parallel workers.
    "test_events": """
        CREATE TABLE {schema}.test_events (
            event_id    BIGINT          NOT NULL GENERATED ALWAYS AS IDENTITY
                        (START WITH 1 INCREMENT BY 1) PRIMARY KEY,
            event_type  VARCHAR(50)     NOT NULL,
            user_id     INTEGER         NOT NULL,
            session_id  VARCHAR(36)     NOT NULL,
            page_url    VARCHAR(200)    NOT NULL,
            referrer    VARCHAR(200),
            duration_ms INTEGER,
            http_status SMALLINT        NOT NULL DEFAULT 200,
            created_at  TIMESTAMP       NOT NULL DEFAULT CURRENT TIMESTAMP
        )
    """,
}

INDEXES: list[str] = [
    # Orders: watermark column must be indexed for efficient incremental queries
    "CREATE INDEX {schema}.idx_orders_updated  ON {schema}.test_orders (updated_at)",
    "CREATE INDEX {schema}.idx_orders_customer ON {schema}.test_orders (customer_id)",
    # Events: supporting indexes for query patterns
    "CREATE INDEX {schema}.idx_events_user     ON {schema}.test_events (user_id)",
    "CREATE INDEX {schema}.idx_events_type     ON {schema}.test_events (event_type)",
    "CREATE INDEX {schema}.idx_events_created  ON {schema}.test_events (created_at)",
]


def table_exists(conn, schema: str, table: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = ?",
        (schema.upper(), table.upper()),
    )
    return cur.fetchone()[0] > 0


def create_tables(conn, schema: str) -> None:
    cur = conn.cursor()
    for name, ddl in DDL.items():
        if table_exists(conn, schema, name):
            print(f"  [skip]   {schema}.{name} — already exists")
            continue
        print(f"  [create] {schema}.{name}")
        cur.execute(ddl.format(schema=schema))

    for idx_sql in INDEXES:
        try:
            cur.execute(idx_sql.format(schema=schema))
        except Exception:
            pass  # index already exists

    conn.commit()
    print("Tables and indexes created.\n")


def drop_tables(conn, schema: str) -> None:
    cur = conn.cursor()
    for name in reversed(list(DDL.keys())):
        if table_exists(conn, schema, name):
            print(f"  [drop] {schema}.{name}")
            cur.execute(f"DROP TABLE {schema}.{name}")
    conn.commit()
    print("Tables dropped.\n")


def count_tables(conn, schema: str) -> None:
    cur = conn.cursor()
    for name in DDL:
        if table_exists(conn, schema, name):
            cur.execute(f"SELECT COUNT(*) FROM {schema}.{name}")
            n = cur.fetchone()[0]
            print(f"  {schema}.{name}: {n:,} rows")
        else:
            print(f"  {schema}.{name}: [does not exist]")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

STATUSES  = ["pending", "processing", "shipped", "delivered", "cancelled", "returned"]
EVENT_TYPES = ["page_view", "click", "search", "add_to_cart", "remove_from_cart",
               "checkout_start", "purchase", "login", "logout", "error"]
PAGES     = ["/home", "/products", "/products/detail", "/cart", "/checkout",
             "/account", "/search", "/about", "/contact", "/blog"]
REFERRERS = ["https://google.com", "https://bing.com", "https://facebook.com",
             "https://twitter.com", None, None, None]   # None = direct traffic
REGIONS   = ["us-east", "us-west", "eu-west", "eu-central", "ap-southeast", "ap-northeast"]
CATEGORIES = ["electronics", "clothing", "home", "sports", "books", "food", "toys", "tools"]


def runstats(conn, schema: str, table: str) -> None:
    """Collect table and index statistics via SYSPROC.ADMIN_CMD after bulk loads."""
    print(f"  Running RUNSTATS on {schema}.{table}...")
    cur = conn.cursor()
    cur.execute(
        f"CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE {schema}.{table} "
        "WITH DISTRIBUTION AND DETAILED INDEXES ALL')"
    )
    conn.commit()
    print(f"  RUNSTATS complete.\n")


def _batched_executemany(conn, sql: str, rows: list, batch_size: int, label: str) -> None:
    cur = conn.cursor()
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        cur.executemany(sql, batch)
        conn.commit()
        total += len(batch)
        pct = total / len(rows) * 100
        print(f"\r  {label}: {total:,} / {len(rows):,}  ({pct:.1f}%)", end="", flush=True)
    print()


# ---------------------------------------------------------------------------
# Scenario 1: test_reference  (full load, ~1 K rows)
# ---------------------------------------------------------------------------

def seed_reference(conn, schema: str, n: int = 1_000) -> None:
    print(f"Seeding {schema}.test_reference ({n:,} rows)...")
    sql = (
        f"INSERT INTO {schema}.test_reference "
        "(ref_id, ref_code, ref_description, category, ref_value, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    rows = [
        (
            i,
            f"REF{i:05d}",
            "Item " + "".join(random.choices(string.ascii_lowercase + " ", k=30)).strip(),
            random.choice(CATEGORIES),
            round(random.uniform(0.01, 9_999.99), 2),
            1 if random.random() > 0.15 else 0,
        )
        for i in range(1, n + 1)
    ]
    _batched_executemany(conn, sql, rows, batch_size=500, label="test_reference")
    print(f"  Done — {n:,} reference rows.")
    runstats(conn, schema, "test_reference")


# ---------------------------------------------------------------------------
# Scenario 2: test_orders  (incremental / watermark, seed + ongoing delta)
# ---------------------------------------------------------------------------

def seed_orders(conn, schema: str, n: int = 50_000) -> None:
    print(f"Seeding {schema}.test_orders ({n:,} rows)...")
    sql = (
        f"INSERT INTO {schema}.test_orders "
        "(customer_id, product_id, quantity, unit_price, total_price, status, region, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    # Spread seed data from 2024-01-01 so the 0001-01-01 watermark start picks it all up
    base_ts = datetime.datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        qty   = random.randint(1, 20)
        price = round(random.uniform(1.99, 499.99), 2)
        ts    = base_ts + datetime.timedelta(seconds=i * 3)
        rows.append((
            random.randint(1, 10_000),
            random.randint(1, 1_000),
            qty,
            price,
            round(qty * price, 2),
            random.choice(STATUSES),
            random.choice(REGIONS),
            ts,  # pass datetime object; ibm_db_dbi handles TIMESTAMP conversion
        ))
    _batched_executemany(conn, sql, rows, batch_size=2_000, label="test_orders")
    print(f"  Done — {n:,} order rows.")
    runstats(conn, schema, "test_orders")


# ---------------------------------------------------------------------------
# Scenario 3: test_events  (partitioned full load, millions of rows)
# ---------------------------------------------------------------------------

def seed_events(conn, schema: str, n: int = 5_000_000, batch_size: int = 1_000) -> None:
    print(f"Seeding {schema}.test_events ({n:,} rows) — this will take several minutes...")
    sql = (
        f"INSERT INTO {schema}.test_events "
        "(event_type, user_id, session_id, page_url, referrer, duration_ms, http_status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    # Spread events from 2023-01-01 so the full range is large and partitioning is meaningful
    base_ts  = datetime.datetime(2023, 1, 1)
    total    = 0
    cur      = conn.cursor()
    start_ts = time.monotonic()

    while total < n:
        chunk = min(batch_size, n - total)
        rows  = []
        for j in range(chunk):
            ts = base_ts + datetime.timedelta(seconds=total + j)
            rows.append((
                random.choice(EVENT_TYPES),
                random.randint(1, 100_000),
                str(uuid.uuid4()),
                random.choice(PAGES),
                random.choice(REFERRERS),
                random.randint(50, 30_000) if random.random() > 0.05 else None,
                200 if random.random() > 0.03 else random.choice([301, 400, 404, 500]),
                ts,  # pass datetime object; ibm_db_dbi handles TIMESTAMP conversion
            ))
        cur.executemany(sql, rows)
        conn.commit()
        total += chunk

        elapsed = time.monotonic() - start_ts
        rate    = total / elapsed if elapsed > 0 else 0
        eta_s   = (n - total) / rate if rate > 0 else 0
        eta_str = f"{int(eta_s // 60)}m{int(eta_s % 60):02d}s" if eta_s > 0 else "—"
        pct     = total / n * 100
        print(
            f"\r  test_events: {total:>9,} / {n:,}  ({pct:.1f}%)  "
            f"{rate:,.0f} rows/s  ETA {eta_str}  ",
            end="", flush=True,
        )

    elapsed = time.monotonic() - start_ts
    print(f"\n  Done — {n:,} event rows in {elapsed:.1f}s  ({n/elapsed:,.0f} rows/s).")
    runstats(conn, schema, "test_events")


# ---------------------------------------------------------------------------
# Ongoing incremental data generator
# ---------------------------------------------------------------------------

def generate_incremental(
    conn,
    schema: str,
    new_orders: int   = 500,
    update_count: int = 200,
    interval_sec: int = 60,
    cycles: int       = 10,
) -> None:
    """
    Simulates real workload activity on test_orders for watermark-based incremental testing:
      - Inserts `new_orders` new rows per cycle (advances MAX(updated_at))
      - Updates `update_count` random existing rows (status change, updated_at refreshed)

    Run this between lakefed_ingest_copy job executions to generate a meaningful delta.
    """
    print(
        f"Incremental generator starting:\n"
        f"  {new_orders} inserts + {update_count} updates per cycle\n"
        f"  {cycles} cycles, {interval_sec}s apart\n"
    )

    insert_sql = (
        f"INSERT INTO {schema}.test_orders "
        "(customer_id, product_id, quantity, unit_price, total_price, status, region, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT TIMESTAMP)"
    )
    update_sql = (
        f"UPDATE {schema}.test_orders "
        "SET status = ?, updated_at = CURRENT TIMESTAMP "
        "WHERE order_id = ?"
    )
    max_id_sql = f"SELECT MAX(order_id) FROM {schema}.test_orders"

    for cycle in range(1, cycles + 1):
        cur = conn.cursor()
        now = datetime.datetime.now().strftime("%H:%M:%S")

        # ── Fetch max_id BEFORE inserts so updates only target pre-existing rows ──
        cur.execute(max_id_sql)
        max_id = cur.fetchone()[0] or 0

        # ── Inserts ──
        insert_rows = []
        for _ in range(new_orders):
            qty   = random.randint(1, 20)
            price = round(random.uniform(1.99, 499.99), 2)
            insert_rows.append((
                random.randint(1, 10_000),
                random.randint(1, 1_000),
                qty,
                price,
                round(qty * price, 2),
                random.choice(STATUSES),
                random.choice(REGIONS),
            ))
        cur.executemany(insert_sql, insert_rows)

        # ── Updates — sample only from rows that existed before this cycle's inserts ──
        actual_updates = 0
        if max_id > 0:
            update_ids  = random.sample(range(1, max_id + 1), min(update_count, max_id))
            update_rows = [(random.choice(STATUSES), oid) for oid in update_ids]
            cur.executemany(update_sql, update_rows)
            actual_updates = len(update_rows)

        conn.commit()
        print(
            f"  Cycle {cycle:>2}/{cycles}  [{now}]  "
            f"+{new_orders} inserts, {actual_updates} updates"
        )

        if cycle < cycles:
            time.sleep(interval_sec)

    print("\nIncremental generation complete.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(env: dict) -> argparse.Namespace:
    # Map .env keys → argument defaults
    p = argparse.ArgumentParser(
        description="DB2 test data generator for lakefed-ingest ingestion scenarios",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Connection — defaults come from .env; CLI flags override
    conn_grp = p.add_argument_group(
        "DB2 connection",
        f"Defaults loaded from {_ENV_PATH} (CLI flags override)",
    )
    conn_grp.add_argument("--host",     default=env.get("hostname"),
                          help="DB2 hostname or IP")
    conn_grp.add_argument("--port",     default=int(env.get("port", 50000)), type=int,
                          help="DB2 port (default: 50000)")
    conn_grp.add_argument("--database", default=env.get("database"),
                          help="DB2 database name")
    conn_grp.add_argument("--user",     default=env.get("username"),
                          help="DB2 username")
    conn_grp.add_argument("--password", default=env.get("password"),
                          help="DB2 password")
    conn_grp.add_argument("--schema",   default=env.get("schema_name"),
                          help="Target schema (e.g. HFT64286)")
    conn_grp.add_argument("--ssl",      default=env.get("ssl", "false").lower() == "true",
                          action=argparse.BooleanOptionalAction,
                          help="Enable SSL (default: from .env ssl field)")

    # Action
    p.add_argument(
        "--action",
        choices=["create", "seed", "generate", "all", "drop", "count"],
        default="all",
        help=(
            "create=DDL only | seed=insert rows | generate=incremental loop | "
            "all=create+seed+generate (default) | drop=drop tables | count=row counts"
        ),
    )

    # Seed sizes
    seed_grp = p.add_argument_group("Seed sizes")
    seed_grp.add_argument("--reference-rows", default=1_000,     type=int, metavar="N",
                          help="Rows for test_reference (default: 1000)")
    seed_grp.add_argument("--orders-rows",    default=50_000,    type=int, metavar="N",
                          help="Seed rows for test_orders (default: 50000)")
    seed_grp.add_argument("--events-rows",    default=800_000,   type=int, metavar="N",
                          help="Rows for test_events (default: 800000; DB2 on Cloud Lite cap ~200MB)")

    # Incremental generator
    gen_grp = p.add_argument_group("Incremental generator (--action generate or all)")
    gen_grp.add_argument("--gen-cycles",   default=10,  type=int, metavar="N",
                         help="Number of cycles (default: 10)")
    gen_grp.add_argument("--gen-interval", default=60,  type=int, metavar="SEC",
                         help="Seconds between cycles (default: 60)")
    gen_grp.add_argument("--gen-inserts",  default=500, type=int, metavar="N",
                         help="New orders per cycle (default: 500)")
    gen_grp.add_argument("--gen-updates",  default=200, type=int, metavar="N",
                         help="Updated orders per cycle (default: 200)")

    args = p.parse_args()

    # Validate required connection fields (may come from .env or CLI)
    missing = [f for f, v in [
        ("--host / .env hostname", args.host),
        ("--database / .env database", args.database),
        ("--user / .env username", args.user),
        ("--password / .env password", args.password),
        ("--schema / .env schema_name", args.schema),
    ] if not v]
    if missing:
        p.error(
            "Missing required connection values (set in scripts/.env or pass as CLI flags):\n  "
            + "\n  ".join(missing)
        )

    return args


def main() -> None:
    env  = load_env()
    if env:
        print(f"Loaded credentials from {_ENV_PATH}")
    else:
        print(f"No {_ENV_PATH} found — expecting CLI flags for connection details.")

    args = parse_args(env)

    ssl_label = " (SSL)" if args.ssl else ""
    print(f"Connecting to {args.host}:{args.port}/{args.database} as {args.user}{ssl_label}...")
    try:
        conn = connect(args.host, args.port, args.database, args.user, args.password, ssl=args.ssl)
    except Exception as exc:
        sys.exit(f"Connection failed: {exc}")
    print("Connected.\n")

    try:
        match args.action:
            case "drop":
                drop_tables(conn, args.schema)

            case "create":
                create_tables(conn, args.schema)

            case "count":
                count_tables(conn, args.schema)

            case "seed":
                seed_reference(conn, args.schema, args.reference_rows)
                seed_orders(conn, args.schema, args.orders_rows)
                seed_events(conn, args.schema, args.events_rows)

            case "generate":
                generate_incremental(
                    conn, args.schema,
                    new_orders=args.gen_inserts,
                    update_count=args.gen_updates,
                    interval_sec=args.gen_interval,
                    cycles=args.gen_cycles,
                )

            case "all":
                create_tables(conn, args.schema)
                seed_reference(conn, args.schema, args.reference_rows)
                seed_orders(conn, args.schema, args.orders_rows)
                seed_events(conn, args.schema, args.events_rows)
                generate_incremental(
                    conn, args.schema,
                    new_orders=args.gen_inserts,
                    update_count=args.gen_updates,
                    interval_sec=args.gen_interval,
                    cycles=args.gen_cycles,
                )

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        conn.close()
        print("Connection closed.")


if __name__ == "__main__":
    main()
