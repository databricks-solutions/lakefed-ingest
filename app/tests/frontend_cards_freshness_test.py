"""The Overview page's Freshness card, as the cards board approved it: the note behind the info icon, the same summary as
the job and database pages (a total, one bar, a legend that links to the table lists), and no late-database line here,
since the Databases tile counts them. Plain source checks on the TSX, as the other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _freshness():
    start = OVERVIEW.index("function Freshness(")
    return OVERVIEW[start:OVERVIEW.index("\n}\n", start)]


def test_freshness_note_sits_behind_the_info_icon_with_the_loaded_meaning():
    card = _freshness()
    assert "<Section title={<>Freshness <Info>Every enabled table of every job" in card
    assert "Loaded counts late tables too." in card and "note=" not in card


def test_freshness_is_the_shared_summary_with_links_to_the_table_lists():
    card = _freshness()
    assert "<StateSummary row={totals} looksGood" in card
    assert 'loaded: "/tables", waiting: "/tables?status=seeding", failing: "/tables?status=failing"' in card
    assert "<StateBar" not in card


def test_freshness_legend_has_no_late_databases_line_or_stacked_zero_counts():
    card = _freshness()
    assert "Late databases" not in card and "late_databases" not in card
    assert "state_loaded" not in card and 'className="key"' not in card
