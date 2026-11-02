"""The empty tables box in New job is clickable: it offers the same choices as Add tables, from a source or a CSV.
Plain source checks, no app run."""
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"


def test_the_empty_tables_box_offers_the_same_choices_as_add_tables():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "const addTableChoices = (<>" in text
    assert text.count("{addTableChoices}") == 2
    assert '<div className="empty-choices">{addTableChoices}</div>' in text
    assert "No tables yet. Add them with <b>Add tables</b>." in text
