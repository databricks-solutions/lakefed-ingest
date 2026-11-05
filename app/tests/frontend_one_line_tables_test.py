"""Every table in the operations app keeps its rows on one line: fixed layout with a column group, long values cut with an
ellipsis and shown whole on hover or focus, and coded values by display name. Plain source checks of the pages and styles,
no browser, database or app run."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
PAGES = sorted((SOURCE / "pages").glob("*.tsx"))
STYLES = (SOURCE / "styles.css").read_text()
COMPONENTS = (SOURCE / "components.tsx").read_text()

# Form tables hold inputs (the identity columns), not rows of values; they keep their natural layout.
FORM_TABLES = {"jobForm.tsx"}


def _tables(text: str) -> list[str]:
    """The opening tag of each plain table in the text."""
    return re.findall(r"<table\b[^>]*>", text)


def test_every_list_table_uses_the_one_line_layout():
    unmarked = []
    for page in PAGES:
        if page.name in FORM_TABLES:
            continue
        unmarked += [f"{page.name}: {tag}" for tag in _tables(page.read_text())
                     if "oneLine(" not in tag and "one-line" not in tag]
    assert not unmarked, unmarked


def test_every_one_line_table_names_its_column_widths():
    for page in PAGES:
        text = page.read_text()
        assert text.count("<Cols widths=") >= text.count("{...oneLine("), page.name


def test_no_table_wraps_its_headings_or_breaks_inside_a_word():
    for page in PAGES:
        assert "wrap-head" not in page.read_text(), page.name
    assert "wrap-head" not in STYLES
    for selector, declarations in re.findall(r"([^{}]*)\{([^}]*)\}", STYLES):
        if "table" in selector and re.search(r"break-all|break-word|overflow-wrap:\s*anywhere|word-break", declarations):
            raise AssertionError(f"a table rule breaks inside a word: {selector.strip()}")


def test_the_one_line_rules_cut_with_an_ellipsis_in_a_fixed_layout():
    assert re.search(r"table\.data\.one-line\s*\{\s*table-layout:\s*fixed", STYLES)
    cells = re.search(r"table\.data\.one-line th,[^{]*\{([^}]*)\}", STYLES)
    assert cells, "no one-line cell rule"
    for declaration in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"):
        assert declaration in cells.group(1), declaration


def test_a_row_menu_is_not_clipped_by_its_cell():
    assert re.search(r"table\.data\.one-line td\.card-actions\s*\{\s*overflow:\s*visible", STYLES)


def test_the_shared_pieces_are_exported_for_the_pages():
    for name in ("Cut", "CutCopy", "CopyButton", "Cols", "oneLine"):
        assert re.search(rf"export function {name}\b", COMPONENTS), name


def test_the_grids_show_a_tooltip_only_for_a_cut_value_and_cap_autosized_columns():
    assert 'tooltipShowMode="whenTruncated"' in COMPONENTS
    assert "enableCellTextSelection" in COMPONENTS
    assert re.search(r"GRID_MAX_COLUMN_WIDTH\s*=\s*\d+", COMPONENTS)
    assert "cellRenderer: timeCellRenderer" in COMPONENTS


def test_the_source_type_names_live_in_one_place():
    copies = [page.name for page in PAGES
              if re.search(r"""["']?sqlserver["']?\s*:\s*["']SQL Server["']""", page.read_text())]
    assert not copies, copies
    assert "SOURCE_TYPE_NAMES" in (SOURCE / "cells.ts").read_text()
