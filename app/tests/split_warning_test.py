"""The warning for a database whose tables sit in more than one ingestion group. The write goes through; the warning
comes back with it, names the databases and groups already read (first three, then a count) and the live jobs holding
those groups with a link to each, and says what a split costs on the source. Plain: a fake cursor stands in for the
database."""
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import main  # noqa: E402
import queries as q  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
TASKS = FRONTEND / "pages" / "tasks.tsx"
COMPONENTS = FRONTEND / "components.tsx"
COST = ("Each group checks the database for changes to its own tables and loads only those, so each extra group adds "
        "its own change queries on the server every cycle and more connections to it at once.")


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


def test_one_database_names_its_groups_and_what_the_split_costs():
    split = [("k1", "sales_01", ["lfc_a", "lfc_b"])]
    assert main._split_message(split) == (
        f"Database sales_01 has tables in more than one ingestion group (lfc_a, lfc_b). {COST}")


def test_several_databases_name_three_and_count_the_rest():
    split = [(f"k{n}", f"db{n}", ["g1"]) for n in range(5)]
    assert main._split_message(split) == (
        f"5 databases have tables in more than one ingestion group: db0, db1, db2 and 2 more. {COST}")


def test_more_than_three_groups_are_counted_too():
    split = [("k1", "sales_01", ["g1", "g2", "g3", "g4", "g5"])]
    assert "(g1, g2, g3 and 2 more)" in main._split_message(split)


def test_a_split_comes_back_as_a_warning_not_a_refusal(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    warnings = main._split_database_warnings(FakeCursor([("k1", "sales_01", ["lfc_a", "lfc_b"])]),
                                             [{"src_database": "sales_01"}])
    assert warnings == [{"kind": "split_database", "message": main._split_message([("k1", "sales_01", ["lfc_a", "lfc_b"])]),
                         "databases": [{"database": "sales_01", "groups": ["lfc_a", "lfc_b"]}], "jobs": []}]


def test_a_database_split_before_the_write_is_no_warning_and_the_others_still_are(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    split = [("k1", "sales_01", ["lfc_a", "lfc_b"]), ("k2", "hr_01", ["lfc_a", "lfc_c"])]
    rows = [{"src_database": "sales_01"}, {"src_database": "hr_01"}]
    [warning] = main._split_database_warnings(FakeCursor(split), rows, already_split={"k1"})
    assert warning["databases"] == [{"database": "hr_01", "groups": ["lfc_a", "lfc_c"]}]
    assert "sales_01" not in warning["message"]
    assert main._split_database_warnings(FakeCursor(split), rows, already_split={"k1", "k2"}) == []


def test_the_databases_split_before_a_write_are_the_keys_the_split_check_finds(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    cursor = FakeCursor([("k1", "sales_01", ["lfc_a", "lfc_b"])])
    assert main._split_database_keys(cursor, [{"src_database": "sales_01"}]) == {"k1"}
    assert cursor.statements == [q.split_databases_sql()]
    assert main._split_database_keys(FakeCursor([]), []) == set()


def test_the_warning_names_the_live_jobs_holding_its_groups_and_gives_their_ids(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    cursor = FakeCursor([("k1", "sales_01", ["lfc_a", "lfc_b"])], jobs=[(7, "Sales"), (9, "Ops")])
    [warning] = main._split_database_warnings(cursor, [{"src_database": "sales_01"}])
    assert warning["jobs"] == [{"id": 7, "name": "Sales"}, {"id": 9, "name": "Ops"}]
    assert "Some of these groups are in ingestion jobs Sales, Ops." in warning["message"]


def test_no_split_is_no_warning_and_the_lookup_of_jobs_runs_only_for_a_split(monkeypatch):
    monkeypatch.setattr(q, "database_key", lambda row: row["src_database"])
    cursor = FakeCursor([])
    assert main._split_database_warnings(cursor, [{"src_database": "sales_01"}]) == []
    assert cursor.statements == [q.split_databases_sql()]
    assert main._split_database_warnings(FakeCursor([]), []) == []


def test_one_live_job_is_named_in_the_singular():
    split = [("k1", "sales_01", ["lfc_a", "lfc_b"])]
    assert main._split_message(split, ["Sales"]) == (
        "Database sales_01 has tables in more than one ingestion group (lfc_a, lfc_b). "
        f"Some of these groups are in ingestion job Sales. {COST}")


def test_the_warnings_of_a_write_in_several_transactions_are_merged_into_one():
    first = main._split_warning([{"database": "db1", "groups": ["g1", "g2"]}], [{"id": 7, "name": "Sales"}])
    second = main._split_warning([{"database": "db1", "groups": ["g3"]}, {"database": "db2", "groups": ["g1", "g2"]}],
                                 [{"id": 7, "name": "Sales"}])
    [merged] = main._merged_warnings([first, second])
    assert merged["databases"] == [{"database": "db1", "groups": ["g1", "g2", "g3"]},
                                   {"database": "db2", "groups": ["g1", "g2"]}]
    assert merged["jobs"] == [{"id": 7, "name": "Sales"}]
    assert merged["message"].startswith("2 databases have tables in more than one ingestion group: db1, db2.")
    assert main._merged_warnings([]) == []


def test_a_split_no_longer_holds_back_finish_on_a_failed_write():
    assert "isClashRefusal" not in TASKS.read_text()


def test_the_warning_box_links_each_job_a_warning_names():
    text = COMPONENTS.read_text()
    assert "export function WriteWarnings(" in text
    assert "Array.isArray(warning.jobs) && warning.jobs.map((job: Row) => (" in text
    assert "Open ingestion job <Link to={`/ingestion-jobs/${job.id}`}>{job.name}</Link>" in text


def test_the_job_page_shows_the_warnings_a_job_keeps_while_it_prepares():
    text = (FRONTEND / "pages" / "IngestionJob.tsx").read_text()
    preparation = text[text.index("function Preparation("):]
    assert "<WriteWarnings warnings={p.warnings} />" in preparation[:preparation.index("\nexport default")]
