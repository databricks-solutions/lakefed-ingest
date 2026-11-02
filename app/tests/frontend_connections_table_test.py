"""The connections table on Add tables, step 1 (Discover.tsx): at a laptop's width (1,280 and 1,440 px; the dialog is
880 px wide at both) it once broke Name (lfc_sqlserver_disco|ver), Type (SQLSE|RVER) and Owner mid-word, because the
dialog body's overflow-wrap: anywhere let the table squeeze those columns to fit a long host that never wraps. Now its
columns are fixed and each value stays on one line, cut with an ellipsis when it does not fit and whole on hover, as
Databricks' tables show long names. Plain source checks, no app run; the captures at both widths are a handback."""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
DISCOVER = (SRC / "pages" / "Discover.tsx").read_text(encoding="utf-8")
STYLES = (SRC / "styles.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    found = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", STYLES)
    assert found, f"no rule for {selector}"
    return re.sub(r"/\*.*?\*/", "", found.group(1), flags=re.S)


def _connections_table() -> str:
    start = DISCOVER.index('<table className="data connections">')
    return DISCOVER[start:DISCOVER.index("</table>", start)]


def test_the_table_lays_out_fixed_columns_so_no_column_is_squeezed():
    assert "table-layout: fixed" in _rule("table.data.connections")
    table = _connections_table()
    assert ('<colgroup><col className="pick" /><col className="name" /><col className="type" /><col />'
            '<col className="owner" /></colgroup>') in table
    assert re.search(r"width:\s*40px", _rule("table.data.connections col.pick"))
    assert re.search(r"width:\s*96px", _rule("table.data.connections col.type"))
    # The host takes what is left: the one column with no width of its own.
    assert re.search(r"width:\s*\d+%", _rule("table.data.connections col.name"))
    assert re.search(r"width:\s*\d+%", _rule("table.data.connections col.owner"))


def test_a_connections_values_stay_on_one_line_and_are_cut_with_an_ellipsis():
    cells = _rule("table.data.connections tr.pick-row td")
    for declaration in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis", "overflow-wrap: normal"):
        assert declaration in cells, declaration
    assert "anywhere" not in cells and "break-all" not in cells


def test_each_value_shows_whole_on_hover():
    table = _connections_table()
    assert '<td className="name" title={c.name}><b>{c.name}</b></td>' in table
    assert "<td title={c.connection_type}>{c.connection_type}</td>" in table
    assert '<td className="mono" title={c.host}>{c.host}</td>' in table
    assert "<td title={c.owner}>{c.owner}</td>" in table


def test_the_line_for_no_connections_still_wraps():
    # Only a connection's own row is held to one line: the explanation when none is found reads as a paragraph.
    assert '<tr key={c.name} className="pick-row"' in _connections_table()
    assert '<td colSpan={5} className="muted">No SQL Server connection' in _connections_table()
