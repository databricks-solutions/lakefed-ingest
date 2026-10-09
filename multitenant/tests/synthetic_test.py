"""Unit tests for the synthetic property-management test-database generator (no database)."""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "synthetic"))

import synth_catalog as pc  # noqa: E402
import synth_sql as ps      # noqa: E402

CAT = pc.build_catalog(42)


def test_catalog_is_deterministic():
    again = pc.build_catalog(42)
    assert [(t.name, t.base_rows, t.column_names, t.pk) for t in CAT] == \
           [(t.name, t.base_rows, t.column_names, t.pk) for t in again]
    assert [t.base_rows for t in pc.build_catalog(7)] != [t.base_rows for t in CAT]


def test_catalog_has_1800_unique_tables_across_18_modules():
    assert len(CAT) == 1800
    assert len({t.name for t in CAT}) == 1800
    assert len({t.module for t in CAT}) == 18


def test_archetype_proportions():
    counts = Counter(t.archetype for t in CAT)
    for arch, share in pc.ARCHETYPE_SHARES:
        assert abs(counts[arch] / 1800 - share) < 0.01, arch


def test_every_table_has_a_pk_and_composite_share():
    assert all(t.pk and set(t.pk) <= set(t.column_names) for t in CAT)
    composite = sum(1 for t in CAT if len(t.pk) > 1) / 1800
    assert 0.13 < composite < 0.17


def test_row_ranges_and_column_counts():
    for t in CAT:
        lo, hi = pc._ROW_RANGES[t.archetype]
        assert lo <= t.base_rows <= hi
        assert 8 <= len(t.columns) <= 140
    assert sum(1 for t in CAT if len(t.columns) >= 100) >= 10      # a few wide masters


def test_scale_factor_and_partitioning():
    s = pc.summarize(CAT, 0.1)
    assert s["total"]["tables"] == 1800
    assert 0 < s["total"]["est_mb"] < 20_000
    part = [t for t in CAT if t.partition_col(0.1)]
    assert part and all(t.partition_col(0.1) == t.pk[0] for t in part)
    assert pc.scaled_partition_size_mb(0.1) == 51


def test_create_table_sql_has_clustered_pk_and_not_null_keys():
    t = next(t for t in CAT if len(t.pk) > 1)
    sql = ps.create_table_sql(t)
    assert sql.startswith(f"CREATE TABLE [dbo].[{t.name}]")
    assert "PRIMARY KEY CLUSTERED ([parent_id], [seq])" in sql
    assert "[parent_id] " in sql and "NOT NULL" in sql


def test_insert_batch_sql_uses_set_based_numbers_source():
    t = next(t for t in CAT if t.archetype == "transaction")
    sql = ps.insert_batch_sql(t, 0, 999_999)
    assert sql.startswith(f"INSERT INTO [dbo].[{t.name}] WITH (TABLOCK)")
    assert "GENERATE_SERIES(CAST(0 AS bigint), CAST(999999 AS bigint))" in sql
    assert sql.count("CASE WHEN") == len(t.columns) - len(t.pk)
    tally = ps.insert_batch_sql(t, 0, 9, use_generate_series=False)
    assert "ROW_NUMBER() OVER" in tally and "TOP (10)" in tally


def test_batches_cover_all_rows_exactly_once():
    t = max(CAT, key=lambda x: x.base_rows)
    b = ps.batches(t, 0.1, batch_rows=1_000_000)
    assert b[0][0] == 0 and b[-1][1] == t.rows(0.1) - 1
    assert all(b[i][1] + 1 == b[i + 1][0] for i in range(len(b) - 1))
    assert ps.batches(next(t for t in CAT if t.archetype == "empty"), 0.1) == []


def test_change_tracking_sql():
    assert ps.enable_ct_database_sql("mydb") == (
        "ALTER DATABASE [mydb] SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 3 DAYS, AUTO_CLEANUP = ON)")
    assert ps.enable_ct_table_sql(CAT[0]).endswith("ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF)")
