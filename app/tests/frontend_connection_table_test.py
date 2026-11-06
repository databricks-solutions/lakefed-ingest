"""Add tables, step 1: the list of connections keeps every row on one line, as Catalog Explorer's connections list and the
Jobs list do. Runs the Node tests of the cell helpers (app/tests/js/cells.test.mjs) and checks the page's source and
styles. Plain source checks, no app run."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
SOURCE = APP / "src" / "frontend" / "src"
TABLE = (SOURCE / "pages" / "ConnectionTable.tsx").read_text()
DISCOVER = (SOURCE / "pages" / "Discover.tsx").read_text()
STYLES = (SOURCE / "styles.css").read_text()
COMPONENTS = (SOURCE / "components.tsx").read_text()


def rule(selector: str) -> str:
    """The declarations of the first rule whose selector list contains `selector` as written."""
    match = re.search(r"(?:^|[},])\s*[^{}]*" + re.escape(selector) + r"[^{}]*\{([^}]*)\}", STYLES)
    assert match, selector
    return match.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_cell_helpers_pass_their_node_tests():
    result = subprocess.run(["node", "--test", str(APP / "tests" / "js" / "cells.test.mjs")],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_discover_lists_the_connections_through_the_table_component():
    assert 'import { ConnectionTable } from "./ConnectionTable";' in DISCOVER
    assert "<ConnectionTable rows={found} chosen={chosen} disabled={testing} onChoose={setChosen}" in DISCOVER
    assert "{c.connection_type}" not in DISCOVER


def test_the_type_column_shows_the_display_name_and_every_text_cell_is_cut_not_wrapped():
    assert "connectionTypeName(c.connection_type)" in TABLE
    for cell in ("c.name", "c.host", "c.owner ?? \"\""):
        assert f"<Cut text={{{cell}}} tipOnlyWhenCut />" in TABLE


def test_the_host_can_be_copied_without_choosing_the_row():
    assert "<CopyButton text={c.host} " in TABLE
    assert "e.stopPropagation();" in COMPONENTS.split("export function CopyButton")[1]


def test_the_table_has_fixed_columns_with_name_and_host_flexible():
    assert "table-layout: fixed" in rule("table.data.connections")
    assert re.fullmatch(r"\s*width: \d+px;\s*", rule(".col-type"))
    assert re.fullmatch(r"\s*width: \d+px;\s*", rule(".col-owner"))
    assert "col-host" not in STYLES    # Host takes what the other columns leave


def test_a_cell_never_breaks_inside_a_word_and_ends_in_an_ellipsis():
    cells = rule("table.data.connections td.cell")
    assert "white-space: nowrap" in cells and "overflow: hidden" in cells
    cut = rule(".host, .cut")
    assert "white-space: nowrap" in cut and "text-overflow: ellipsis" in cut and "overflow: hidden" in cut


def test_the_shared_table_rules_and_the_other_host_cells_are_as_they_were():
    assert "<Cut text={hostLabel(host, port)} className=\"host\" />" in COMPONENTS
    assert 'tipOnlyWhenCut' not in COMPONENTS.split("export function Host")[1].split("export function CopyButton")[0]
    assert "table.data { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }" in STYLES
