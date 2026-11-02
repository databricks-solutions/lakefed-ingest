"""The Overview page's Next scheduled card, as the cards board approved it: a headline count of triggers ("5+" when the
overview's list is full), "No trigger scheduled" with the paused jobs as a short line when there are none, and the
"paused jobs not shown" note behind the info icon on the title. Plain source checks on the TSX, as the other frontend
tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _card():
    start = OVERVIEW.index("function NextScheduled(")
    return OVERVIEW[start:OVERVIEW.index("\n}\n", start)]


def test_next_scheduled_headline_counts_the_triggers_and_marks_a_full_list():
    card = _card()
    assert "const headline = shown >= SHOWN_TRIGGERS ? `${fmtInt(shown)}+` : fmtInt(shown);" in card
    assert "{headline} {plural(shown, \"trigger\")}" in card
    assert "const SHOWN_TRIGGERS = 5;" in OVERVIEW


def test_next_scheduled_with_no_trigger_says_so_and_names_the_paused_jobs():
    card = _card()
    assert '<p className="muted">No trigger scheduled.</p>' in card
    assert '{!shown && paused > 0 && <p className="muted small">' in card


def test_next_scheduled_keeps_the_paused_note_behind_the_info_icon_on_the_title():
    card = _card()
    assert "<Section title={<>Next scheduled{shown > 0 && paused > 0 && <>" in card
    assert "not shown.</Info>" in card and "paused jobs not shown" not in card
