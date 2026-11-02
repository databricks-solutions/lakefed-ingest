"""Postgres types each statement parameter from where it is used, and one passed to a function that takes arguments of
any type (concat, concat_ws, format, json_build_object, ...) can't be typed: the whole statement is refused ("could
not determine data type of parameter"). The unit tests' fakes never prepare a statement, so this reads the SQL the app
and the engine send and finds such a parameter without a cast. It is how Cancel run's close-out and the poller's
reconcile failed on every call (queries.close_ended_run_sql), as the engine's close_abandoned_runs had before."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "app" / "src" / "queries.py", ROOT / "app" / "src" / "main.py", ROOT / "app" / "src" / "run_poller.py",
           ROOT / "src" / "lakefed_ingest" / "state_store.py", ROOT / "src" / "lakefed_ingest" / "delete_job.py"]
ANY_TYPE_CALL = re.compile(r"\b(concat|concat_ws|format|json_build_object|jsonb_build_object|json_build_array|"
                           r"jsonb_build_array)\(([^()]*)\)", re.IGNORECASE)
UNTYPED = re.compile(r"%s(?!::)")


def untyped_parameters(sql):
    """The calls in sql that pass an untyped %s to a function taking arguments of any type."""
    return [match.group(0) for match in ANY_TYPE_CALL.finditer(sql) if UNTYPED.search(match.group(2))]


def test_the_rule_finds_an_untyped_parameter_and_passes_a_typed_one():
    assert untyped_parameters("concat(e.error, ' | ', %s)") == ["concat(e.error, ' | ', %s)"]
    assert untyped_parameters("concat(e.error, ' | ', %s::text)") == []
    assert untyped_parameters("jsonb_build_object('a', %s::int, 'b', %s)") == ["jsonb_build_object('a', %s::int, 'b', %s)"]
    assert untyped_parameters("coalesce(x, %s)") == []


def test_no_statement_passes_an_untyped_parameter_to_a_function_of_any_type():
    found = [f"{path.relative_to(ROOT)}: {call}" for path in SOURCES for call in untyped_parameters(path.read_text())]
    assert found == []
