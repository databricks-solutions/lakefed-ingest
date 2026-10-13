"""FastAPI backend (main.py) with a fake database: bulk register merge / dry run, validation, SPA.

Skipped when FastAPI isn't installed (the repo's base test environment needs only pytest);
run with the app's requirements installed: `pip install -r app/src/requirements.txt httpx pytest`."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

DISCOVERED = [
    dict(discovery_id="d1", src_database="db1", src_schema="dbo", src_table="orders", row_count=10, size_mb=2048.0,
         ct_enabled=True, primary_key="id", select_list="id, v", excluded_columns=None, rowversion_column=None,
         identity_column=None, load_type="ct", cursor_column=None, partition_candidate="id", notes=None,
         src_type="sqlserver", src_host="h", src_port=None, secret_key="k-db1", registered_id=None),
    dict(discovery_id="d1", src_database="db1", src_schema="dbo", src_table="lines", row_count=5, size_mb=1.0,
         ct_enabled=False, primary_key=None, select_list="a, b", excluded_columns=None, rowversion_column=None,
         identity_column=None, load_type="full", cursor_column=None, partition_candidate=None, notes=None,
         src_type="sqlserver", src_host="h", src_port=None, secret_key="k-db1", registered_id=None),
]


CONTROL = {
    7: dict(id=7, task_collection="c1", task_enabled=True, tier=None, priority=None, src_type="sqlserver", src_host="h",
            src_port=None, secret_key="k", src_database="db1", src_schema="dbo", src_table="orders", select_list="id, v",
            primary_key="id", load_type="ct", cursor_column=None, sink_catalog="main", sink_schema="bronze",
            sink_table="orders", client_id=None, load_partitioned=False, partition_col=None, partition_size_mb=None,
            jdbc_options=None),
}
CONTROL[8] = dict(CONTROL[7], id=8, src_table="lines", sink_table="lines")


class FakeCursor:
    """Just enough of a psycopg cursor for main.py's transactional paths."""
    def __init__(self, db):
        self.db, self.description, self._rows, self.rowcount = db, None, [], 0
        self.connection = type("C", (), {"rollback": lambda s: db.rolled_back.append(True)})()

    def execute(self, sql, params=()):
        self.db.statements.append((sql, params))
        self._rows, self.description = [], None
        if "for update" in sql:
            ids = params[0]
            rows = [CONTROL[i] for i in sorted(ids) if i in CONTROL]
            cols = list(next(iter(CONTROL.values())).keys())
            self.description = [type("D", (), {"name": c})() for c in cols]
            self._rows = [tuple(r[c] for c in cols) for r in rows]
        elif "pg_try_advisory_xact_lock" in sql:
            self._rows = [(params[0] not in self.db.running,)]
        elif sql.startswith("insert into lakefed_ingest.control ("):
            if self.db.unique_violation:
                import psycopg
                raise psycopg.errors.UniqueViolation("duplicate")
            table = params[q.CONTROL_INSERT_COLUMNS.index("src_table")]
            self._rows = [] if table == "lines" else [(100 + len(self.db.statements),)]
        elif sql.startswith("update lakefed_ingest.control set") and self.db.unique_violation:
            import psycopg
            raise psycopg.errors.UniqueViolation("duplicate")
        self.rowcount = 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDB:
    def __init__(self):
        self.statements, self.rolled_back, self.running, self.unique_violation = [], [], set(), False

    def of(self, prefix):
        return [p for sql, p in self.statements if sql.startswith(prefix)]


@pytest.fixture
def client(monkeypatch):
    import contextlib
    fake = FakeDB()
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): DISCOVERED if "discovered_table" in sql else [])
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield FakeCursor(fake))))
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db = fake
    return c


def test_review_hides_server_fields_and_partitions_large_tables(client):
    rows = client.post("/api/discoveries/d1/review", json={"shared": {"sink_schema": "bronze"}}).json()
    assert {r["src_table"]: r["load_partitioned"] for r in rows} == {"orders": True, "lines": False}
    assert not any(k.startswith("_") for r in rows for k in r)


def test_register_dry_run_inserts_nothing(client):
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "dry_run": True}).json()
    assert r["counts"] == {"valid": 2} and client.db.of("insert into lakefed_ingest.control (") == []


def test_register_applies_only_editable_fields_and_reports_skips(client):
    edits = [{"src_database": "db1", "src_schema": "dbo", "src_table": "orders", "include": True,
              "sink_table": "ord", "secret_key": "stolen", "_secret_key": "stolen"}]
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "edits": edits}).json()
    assert r["counts"] == {"registered": 1, "skipped": 1}
    inserted = [dict(zip(q.CONTROL_INSERT_COLUMNS, p)) for p in client.db.of("insert into lakefed_ingest.control (")]
    orders = next(i for i in inserted if i["src_table"] == "orders")
    assert orders["sink_table"] == "ord" and orders["secret_key"] == "k-db1" and orders["load_partitioned"] is True
    assert [p[0:2] for p in client.db.of("insert into lakefed_ingest.control_change")] == [("op@example.com", "register")]


def test_register_requires_collection_and_reports_invalid_rows(client):
    assert client.post("/api/discoveries/d1/register", json={"task_collection": ""}).status_code == 422
    edits = [{"src_database": "db1", "src_schema": "dbo", "src_table": "orders", "load_type": "cursor"}]
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "edits": edits, "dry_run": True}).json()
    assert r["counts"] == {"invalid": 1, "valid": 1}
    assert "cursor_column is required for load_type cursor" in r["results"][0]["errors"]


def test_add_table_validation_error_is_422(client):
    r = client.post("/api/tables", json={"task_collection": "c"})
    assert r.status_code == 422 and "src_host is required" in r.json()["detail"]["errors"]


def test_bad_status_filter_is_400_and_unknown_api_is_404(client):
    assert client.get("/api/tables?status=bogus").status_code == 400
    assert client.get("/api/nope").status_code == 404



# ---- editing ----------------------------------------------------------------------------------------

def _edit(client, ids, changes, dry_run=False):
    return client.post("/api/tables/edit", json={"ids": ids, "changes": changes, "dry_run": dry_run})


def test_edit_preview_writes_nothing(client):
    r = _edit(client, [7], {"sink_table": "orders2"}, dry_run=True).json()
    assert r["summary"]["applied"] is False and r["plans"][0]["reseed"] == "sink_changed"
    assert client.db.of("update lakefed_ingest.control set") == []


def test_sink_edit_flips_to_seeding_and_logs(client):
    r = _edit(client, [7], {"sink_table": "orders2"}).json()
    assert r["summary"] == {"rows": 1, "changed": 1, "reseed": 1, "errors": 0, "applied": True}
    assert client.db.of("update lakefed_ingest.control set sink_table = %s") == [["orders2", 7]]
    assert client.db.of("update lakefed_ingest.checkpoint set phase = 'seeding'") == [("reseed after edit: sink_changed", [7])]
    log = client.db.of("insert into lakefed_ingest.control_change")
    assert log[0][:8] == ("op@example.com", "edit", 7, "c1", "sink_table", "orders", "orders2", "sink_changed")


def test_bulk_move_takes_both_collection_locks_and_does_not_reseed(client):
    r = _edit(client, [7, 8], {"task_collection": "c2"}).json()
    assert r["summary"]["applied"] and r["summary"]["reseed"] == 0
    locks = [p[0] for p in client.db.of("select pg_try_advisory_xact_lock")]
    assert locks == ["lakefed_ingest:c1", "lakefed_ingest:c2"]
    assert client.db.of("update lakefed_ingest.checkpoint") == []


def test_edit_refused_while_collection_runs(client):
    client.db.running = {"lakefed_ingest:c2"}
    r = _edit(client, [7], {"task_collection": "c2"})
    assert r.status_code == 409 and "collection 'c2' is running" in r.json()["detail"]


def test_bulk_edit_rejects_single_row_fields_and_invalid_rows(client):
    r = _edit(client, [7, 8], {"primary_key": "id, v"}).json()
    assert r["summary"]["applied"] is False and "cannot be changed in bulk: primary_key" in r["plans"][0]["errors"][0]
    r = _edit(client, [7], {"load_type": "cursor"}).json()
    assert r["summary"]["applied"] is False and "cursor_column is required for load_type cursor" in r["plans"][0]["errors"]
    assert client.db.of("update lakefed_ingest.control set") == []


def test_edit_sink_conflict_is_409_and_nothing_changes(client):
    client.db.unique_violation = True
    r = _edit(client, [7], {"sink_table": "lines"})
    assert r.status_code == 409 and "already writes that sink" in r.json()["detail"]


def test_enable_and_reseed_are_locked_and_logged(client):
    assert client.post("/api/tables/enable", json={"ids": [7], "enabled": False}).json() == {"updated": 1}
    assert client.post("/api/tables/reseed", json={"ids": [7, 8]}).status_code == 200
    actions = [p[1] for p in client.db.of("insert into lakefed_ingest.control_change")]
    assert actions == ["disable", "reseed", "reseed"]
    client.db.running = {"lakefed_ingest:c1"}
    assert client.post("/api/tables/reseed", json={"ids": [7]}).status_code == 409
