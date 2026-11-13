"""The run preview behind Run now's confirmation (main.ingestion_job_run_preview) with a stubbed database, the shape of
its SQL, and a guard that no confirmation in the app talks about cost. The text itself is tested in
tests/js/run_dialog.test.mjs; the SQL against Postgres is run_preview_pg_test.py."""
import re
from pathlib import Path

import pytest

import queries as q

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

FRONTEND = Path(__file__).resolve().parent.parent / "src" / "frontend" / "src"
COST_WORDS = re.compile(r"costs? money", re.IGNORECASE)
COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _preview(monkeypatch, groups, rows):
    seen = []
    monkeypatch.setattr(main, "_job_groups", lambda job_id: groups)

    def query(sql, params=()):
        seen.append((sql, params))
        return rows

    monkeypatch.setattr(main.db, "query", query)
    return TestClient(main.app).get("/api/ingestion-jobs/5/run-preview"), seen


def test_the_preview_counts_the_tables_and_names_the_first_few(monkeypatch):
    row = {"tables": 1002, "servers": 1, "databases": 1, "server": "lfc_sqlserver_discover", "database": "lfc_eng_001",
           "names": ["dbo.a", "dbo.b", "dbo.c"]}
    response, seen = _preview(monkeypatch, ["g1"], [row])
    assert response.json() == {"tables": 1002, "servers": 1, "databases": 1, "server": "lfc_sqlserver_discover",
                               "database": "lfc_eng_001", "names": ["dbo.a", "dbo.b", "dbo.c"]}
    assert seen == [(q.run_preview_sql(), (["g1"], main.RUN_PREVIEW_NAMES))]


def test_a_job_with_no_groups_reads_nothing_and_loads_nothing(monkeypatch):
    response, seen = _preview(monkeypatch, [], [])
    assert response.json() == {"tables": 0, "servers": 0, "databases": 0, "server": None, "database": None, "names": []}
    assert seen == []


def test_the_sql_reads_only_enabled_control_rows_and_takes_a_few_names():
    sql = q.run_preview_sql()
    assert "task_enabled" in sql and "task_collection = any(%s)" in sql and "limit %s" in sql
    assert "checkpoint" not in sql, "the preview must stay a read of the control rows alone"
    assert sql.count("%s") == 2


def test_no_page_text_says_a_run_costs_money():
    offenders = []
    for path in sorted(FRONTEND.rglob("*.ts*")):
        for number, line in enumerate(COMMENTS.sub("", path.read_text()).splitlines(), 1):
            if COST_WORDS.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert not offenders, "a confirmation names what it acts on and says nothing of cost:\n" + "\n".join(offenders)
