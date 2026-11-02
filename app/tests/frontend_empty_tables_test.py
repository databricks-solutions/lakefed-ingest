"""The empty tables box in New job is one clickable target: the whole box is the Add tables menu's button, so a click
(or Enter or Space, as it is a button) opens the same choices as Add tables, from a source or a CSV. Plain source
checks, no app run."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
PAGES = SRC / "pages"


def test_the_empty_tables_box_is_the_add_tables_menu_opened_from_the_whole_box():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "const addTableChoices = (<>" in text
    menus = re.findall(r'<Menu label=\{<>(.*?)</>\}\s+buttonClass="([\w-]+)" title="Add tables">\{addTableChoices\}</Menu>',
                       text, re.S)
    assert [button for _, button in menus] == ["ghost", "empty-drop"], menus
    assert "No tables yet. Click to add them: discover on a source or import a CSV." in menus[1][0]
    # No separate buttons in the box any more: the choices are only in the menu.
    assert "empty-choices" not in text and text.count("{addTableChoices}") == 2


def test_the_menu_button_is_the_whole_box_and_its_choices_drop_below_it():
    css = (SRC / "styles.css").read_text()
    assert ".menu:has(> button.empty-drop) { display: flex; }" in css
    assert "button.empty-drop { width: 100%; height: auto; background: transparent; cursor: pointer; }" in css
    assert ".empty-drop { border: 1px dashed" in css
    assert ".empty-choices" not in css
    # The Menu's trigger is a real button, so the box is reached with Tab and opened with Enter or Space.
    components = (SRC / "components.tsx").read_text()
    assert '<button className={buttonClass} aria-haspopup="menu" aria-expanded={open}' in components
