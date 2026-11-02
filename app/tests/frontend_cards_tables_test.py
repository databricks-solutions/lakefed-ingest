"""The Overview page's Tables tile, as the cards board approved it: the enabled count and the loaded percentage stay,
with no bar here (the Freshness card below holds the one table bar), and "No tables yet" when nothing is enabled.
Plain source checks on the TSX, as the other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _tile(label):
    start = OVERVIEW.index(f'label: "{label}"')
    return OVERVIEW[start:OVERVIEW.index('{ label: "', start + 1)]


def test_tables_tile_says_no_tables_yet_when_nothing_is_enabled():
    tile = _tile("Tables")
    assert 'empty: enabled ? undefined : "No tables yet"' in tile
    assert "{pct}% loaded" in tile


def test_tables_tile_has_no_bar_because_the_freshness_card_draws_the_table_bar():
    tile = _tile("Tables")
    assert "CountBar" not in tile and "info:" not in tile
    assert "CountBar" in _tile("Ingestion jobs") and "CountBar" in _tile("Databases")
