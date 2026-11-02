"""The refusal for a database whose tables sit in more than one ingestion group: its wording names the databases and
groups already read (first three, then a count), names the live jobs that hold some of those groups with a link to each,
says nothing was written, and is the message tasks.tsx turns into no Finish button. Plain: a fake cursor stands in for
the database."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import main  # noqa: E402
import queries as q  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
TASKS = FRONTEND / "pages" / "tasks.tsx"
COMPONENTS = FRONTEND / "components.tsx"


class FakeCursor:
    """Answers the split check with ``split`` and the live-jobs lookup with ``jobs``; records the statements."""

    def __init__(self, split, jobs=()):
        self.split, self.jobs = split, jobs
        self.statements = []
        self.answer = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        self.answer = self.jobs if sql == q.clash_jobs_sql() else self.split

    def fetchall(self):
        return self.answer


def test_one_database_names_its_groups_and_says_nothing_was_written():
    split = [("k1", "sales_01", ["lfc_a", "lfc_b"])]
    assert main._split_message(split) == ("Database sales_01 has tables in more than one ingestion group (lfc_a, lfc_b). "
                                          "Move all of its tables into one group. Nothing was written.")


def test_several_databases_name_three_and_count_the_rest():
    split = [(f"k{n}", f"db{n}", ["g1"]) for n in range(5)]
    assert main._split_message(split) == ("5 databases have tables in more than one ingestion group: db0, db1, db2 and 2 more. "
                                          "Move all of each database's tables into one group. Nothing was written.")


def test_more_than_three_groups_are_counted_too():
    split = [("k1", "sales_01", ["g1", "g2", "g3", "g4", "g5"])]
    assert "(g1, g2, g3 and 2 more)" in main._split_message(split)


def test_the_refusal_is_a_409_with_that_wording_and_no_job_when_none_holds_the_groups(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    with pytest.raises(main.HTTPException) as refused:
        main._refuse_split_databases(FakeCursor([("k1", "sales_01", ["lfc_a", "lfc_b"])]), [{"src_database": "sales_01"}])
    assert refused.value.status_code == 409
    message = main._split_message([("k1", "sales_01", ["lfc_a", "lfc_b"])])
    assert refused.value.detail == {"message": message, "jobs": []}


def test_the_refusal_names_the_live_jobs_holding_its_groups_and_gives_their_ids(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    split = [("k1", "sales_01", ["lfc_a", "lfc_b"])]
    cursor = FakeCursor(split, jobs=[(7, "Sales"), (9, "Ops")])
    with pytest.raises(main.HTTPException) as refused:
        main._refuse_split_databases(cursor, [{"src_database": "sales_01"}])
    assert refused.value.detail["jobs"] == [{"id": 7, "name": "Sales"}, {"id": 9, "name": "Ops"}]
    assert "Some of these groups are in ingestion jobs Sales, Ops." in refused.value.detail["message"]


def test_the_lookup_of_jobs_runs_only_when_the_split_check_finds_a_database(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    cursor = FakeCursor([])
    main._refuse_split_databases(cursor, [{"src_database": "sales_01"}])
    assert cursor.statements == [q.split_databases_sql()]


def test_one_live_job_is_named_in_the_singular():
    split = [("k1", "sales_01", ["lfc_a", "lfc_b"])]
    assert main._split_message(split, ["Sales"]) == (
        "Database sales_01 has tables in more than one ingestion group (lfc_a, lfc_b). "
        "Some of these groups are in ingestion job Sales. Move all of its tables into one group. Nothing was written.")


def test_a_background_task_keeps_the_message_not_the_detail_dict():
    error = main.HTTPException(409, {"message": "Database sales_01 has tables in more than one ingestion group (a, b). "
                                                "Nothing was written.", "jobs": [{"id": 7, "name": "Sales"}]})
    assert main._error_text(error) == ("Database sales_01 has tables in more than one ingestion group (a, b). "
                                       "Nothing was written.")


def test_the_wording_matches_the_pattern_that_hides_finish():
    pattern = re.search(r"isClashRefusal = .*?/(tables in more than one ingestion group)/\.test", TASKS.read_text()).group(1)
    assert re.search(pattern, main._split_message([("k1", "sales_01", ["a"])]))
    assert re.search(pattern, main._split_message([(f"k{n}", f"db{n}", ["a"]) for n in range(4)]))
    assert not re.search(pattern, "Stopped with 2 of 5 databases written")


def test_the_error_box_links_each_job_a_refusal_names():
    text = COMPONENTS.read_text()
    assert "Array.isArray(e.detail?.jobs) && e.detail.jobs.map((job: Row) => (" in text
    assert "Open ingestion job <Link to={`/ingestion-jobs/${job.id}`}>{job.name}</Link>" in text
