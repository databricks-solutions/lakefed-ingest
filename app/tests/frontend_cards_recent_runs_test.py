"""The Overview page's Recent runs card: the run count as a link to the runs, one bar segment per run by outcome, a legend
only when a run failed or succeeded with failures, and the "each job's last run" note behind the info icon. No per-run
rows: the card is a number and a link. Plain source checks on the TSX, as the other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _card():
    start = OVERVIEW.index("function RecentRuns(")
    return OVERVIEW[start:OVERVIEW.index("\n}\n", start)]


def test_recent_runs_note_sits_behind_the_info_icon_and_the_all_runs_link_stays():
    card = _card()
    assert "<Section title={<>Recent runs <Info>Each job's last ingestion run.</Info></>}" in card
    assert 'actions={<Link to="/runs">All runs</Link>}' in card
    assert "note=" not in card


def test_recent_runs_headline_is_a_link_that_counts_the_listed_runs_and_marks_a_full_list():
    card = _card()
    assert "const headline = last > shown ? `${fmtInt(shown)}+` : fmtInt(shown);" in card
    assert '<Link to="/runs">{headline} {plural(shown, "run")}</Link>' in card
    assert "const SHOWN_RECENT_RUNS = 6;" in OVERVIEW


def test_recent_runs_bar_colours_outcomes_and_shows_its_legend_only_with_failures():
    card = _card()
    assert '{ count: countTone("loaded"), word: "succeeded", tone: "loaded" }' in card
    assert 'const OUTCOME_TONES: Record<string, Tone> = { ok: "loaded", warn: "late", bad: "failing", run: "running" };' in OVERVIEW
    assert 'legend={countTone("late") + countTone("failing") > 0}' in card


def test_recent_runs_has_no_per_run_rows():
    card = _card()
    assert "attention-row" not in card and "<RunStatus" not in card
