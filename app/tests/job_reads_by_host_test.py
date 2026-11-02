"""An ingestion job's settings say whether any of its tables is read by host and password (the Manage export's question),
so Edit shows the trust and secret scope settings only for such a job. Plain: a fake fetch stands in for Lakebase."""
import contextlib

import pytest

pytest.importorskip("fastapi")

import main  # noqa: E402
import queries as q  # noqa: E402


def test_the_host_read_question_is_the_export_query_over_the_jobs_groups():
    asked = []

    def fetch(sql, params=()):
        asked.append((sql, params))
        return [{"jdbc": True}]

    assert main._reads_by_host(fetch, ["sales_1"]) is True
    assert asked == [(q.export_reads_jdbc_sql(), (["sales_1"],))]


@pytest.mark.parametrize("jdbc", [True, False])
def test_the_settings_answer_says_whether_a_table_is_read_by_host(monkeypatch, jdbc):
    @contextlib.contextmanager
    def snapshot():
        yield lambda sql, params=(): [{"n": 2}] if sql == q.groups_row_count_sql() else [{"jdbc": jdbc}]

    monkeypatch.setattr(main, "_job_groups", lambda job_id: ["sales_1"])
    monkeypatch.setattr(main, "_snapshot", snapshot)
    monkeypatch.setattr(main, "_load_schedule", lambda job_id: [{}])
    monkeypatch.setattr(main, "_unity_catalog", lambda job: False)
    monkeypatch.setattr(main.jss, "read", lambda fetch, groups, unity_catalog: {"identity": {}})
    assert main.ingestion_job_settings(7)["reads_by_host"] is jdbc


def test_a_job_with_no_groups_reads_no_table_by_host(monkeypatch):
    monkeypatch.setattr(main, "_job_groups", lambda job_id: [])
    assert main.ingestion_job_settings(7)["reads_by_host"] is False
