"""The ingestion job page's metrics cards, as the cards board approved them: Tables now, Last ingestion run, Next
trigger, Speed and Ingestion run time. Plain source checks on the TSX and CSS, as the other frontend tests are."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
COMPONENTS = (SOURCE / "components.tsx").read_text()
STYLES = (SOURCE / "styles.css").read_text()
JOB = (SOURCE / "pages" / "IngestionJob.tsx").read_text()
DATABASE = (SOURCE / "pages" / "Database.tsx").read_text()


def _function(source, name):
    start = source.index(f"export function {name}(")
    return source[start:source.index("\nexport ", start + 1)]


def _rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLES)
    assert match, selector
    return match.group(1)


def test_tables_now_is_a_total_a_bar_and_a_legend_with_no_all_loaded_line():
    summary = _function(COMPONENTS, "StateSummary")
    assert "No tables yet" in summary and "Looks good" in summary
    assert not re.search(r"All \d|All loaded|All \$\{", summary)
    assert '<StateSummary row={j} looksGood lateDatabases />' in JOB
    assert "StateBar row={j} full" not in JOB
    assert "Loaded counts late tables too." in JOB
    assert "<StateSummary row={d} looksGood />" in DATABASE and "StateBar" not in DATABASE


def test_tables_now_writes_late_databases_under_the_legend_only_above_zero():
    summary = _function(COMPONENTS, "StateSummary")
    assert "lateDatabases && late > 0" in summary
    assert "late {plural(late, \"database\")}" in summary


def test_tables_now_legend_has_one_wrapping_line_beside_each_swatch():
    assert "display: flex; flex-direction: column" in _rule(".summary")
    legend = _rule(".summary .key")
    assert "align-items: center" in legend and "margin-top: 0" in legend
    assert "white-space: nowrap" in _rule(".summary .key > span")
    assert "color: var(--s-loaded)" in _rule(".key .all-done svg")


def _metric(label, following):
    start = JOB.index(f'<div className="metric-label">{label}')
    return JOB[start:JOB.index(f'<div className="metric-label">{following}', start)]


def test_last_ingestion_run_joins_its_time_and_duration_with_a_dot():
    card = _metric("Last ingestion run", "Next trigger")
    assert "` · took ${fmtDuration(last.took_s)}`" in card
    assert ", took" not in card and "Never run" in card


def test_next_trigger_says_not_scheduled_and_keeps_its_notes_behind_the_info_icon():
    card = _metric("Next trigger", "Speed")
    assert '"Not scheduled"' in card and "–" not in card
    assert "Its first group starts then." in card and "No trigger fires until it is resumed." in card
    assert "metric-sub" not in card and "muted" in card


def test_speed_says_not_yet_and_counts_its_median_runs_with_the_notes_behind_the_info_icon():
    card = _metric("Speed", "Ingestion run time")
    assert ">Not yet</div>" in card and "–" not in card
    assert "Median of the last {fmtInt(j.speed_job_runs)} {plural(j.speed_job_runs, \"run\")}" in card
    assert "the median of the last" in card and "No ingestion run loaded tables in 7 days." in card
    assert "tables loaded, median of the last" not in card


def test_ingestion_run_time_drops_the_p95_line_and_says_not_yet():
    start = JOB.index('<div className="metric-label">Ingestion run time')
    card = JOB[start:JOB.index("{live.data?.active", start)]
    assert "p95 over 7 days" not in card and "–" not in card
    assert "95% of ingestion runs took this long or less, over the last 7 days." in card
    assert ">Not yet</div>" in card and "Shows once an ingestion run has finished." in card
