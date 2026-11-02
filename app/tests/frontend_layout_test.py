"""The rules that keep a dialog's actions on screen (src/frontend/src/styles.css): the dialog is never taller than the
window, its body scrolls, and its title and actions don't shrink. A long body (101 unread databases, each with its
error) once pushed Add the tables chosen out of reach. The browser check of the same is scratch's layout_check.sh,
run in headless Chrome against the local mock."""
import re
from pathlib import Path

STYLES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "styles.css"


def _rule(selector: str) -> str:
    css = STYLES.read_text(encoding="utf-8")
    found = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"no rule for {selector}"
    return re.sub(r"/\*.*?\*/", "", found.group(1), flags=re.S)


def test_a_dialog_is_never_taller_than_the_window_and_lays_out_as_a_column():
    rule = _rule(".modal")
    assert re.search(r"max-height:\s*calc\(100vh", rule)
    assert "display: flex" in rule and "flex-direction: column" in rule


def test_a_dialogs_body_scrolls_and_its_title_and_actions_stay():
    body = _rule(".modal-body")
    assert "overflow: auto" in body and "min-height: 0" in body
    assert "flex-shrink: 0" in _rule(".modal h2, .modal-actions")


def test_the_unread_databases_list_scrolls_in_its_own_area():
    assert "overflow: auto" in _rule(".unread-all ul") and "max-height" in _rule(".unread-all ul")


def test_a_dialog_is_centred_by_margins_and_never_wider_than_the_window():
    """Grid centring ran a dialog wider than its window off the left edge; auto margins start it at the edge instead."""
    assert "display: flex" in _rule(".modal-backdrop") and "place-items" not in _rule(".modal-backdrop")
    rule = _rule(".modal")
    assert "margin: auto" in rule and re.search(r"max-width:\s*calc\(100vw", rule) and "overflow: hidden" in rule
    assert "overflow-wrap: anywhere" in _rule(".modal-body")
