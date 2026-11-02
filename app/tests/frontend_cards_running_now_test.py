"""The Overview page's Running now card: a headline count that opens the runs list, a note about groups in no job, and the
meaning behind the info icon. No per-run rows (those are on the runs list and the job pages). The bar components keep their
shape for the job page. Plain source checks on the TSX, as the other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
COMPONENTS = (SOURCE / "components.tsx").read_text()
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _function(source, name):
    start = source.index(f"export function {name}(")
    return source[start:source.index("\nexport ", start + 1)]


def test_running_now_headline_is_a_count_that_opens_the_runs_list():
    card = _function(OVERVIEW, "RunningNow")
    assert '<Link to="/runs">{fmtInt(total)} {plural(total, "ingestion run")}</Link>' in card
    assert "<Info>Ingestion runs going now, and runs of groups that belong to no job.</Info>" in card


def test_running_now_has_no_per_run_rows_or_progress_bars():
    card = _function(OVERVIEW, "RunningNow")
    assert "run-row" not in card and "<RunBar" not in card
    assert "progressFrom" not in OVERVIEW and "ProgressBar" not in OVERVIEW


def test_run_bar_draws_done_in_flight_and_failed_with_the_rest_as_the_track():
    run_bar = _function(COMPONENTS, "RunBar")
    assert 'part.tone !== "notrun"' in run_bar
    assert "whole={p.total}" in run_bar
    assert "All " not in run_bar and "All N" not in run_bar


def test_count_bar_can_draw_against_a_whole_larger_than_its_parts():
    count_bar = _function(COMPONENTS, "CountBar")
    assert "whole ?? 0" in count_bar
