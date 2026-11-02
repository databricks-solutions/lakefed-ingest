"""The Overview page's Databases tile, as the cards board approved it: a bar of late and not-late databases, the server
count behind the info icon, and the late databases gone from the Freshness legend so each count shows once. Plain
source checks on the TSX, as the other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _tile(label):
    start = OVERVIEW.index(f'label: "{label}"')
    return OVERVIEW[start:OVERVIEW.index('{ label: "', start + 1)]


def _freshness():
    start = OVERVIEW.index("function Freshness(")
    return OVERVIEW[start:OVERVIEW.index("\n}\n", start)]


def test_databases_tile_is_a_bar_of_not_late_and_late_databases():
    tile = _tile("Databases")
    assert 'word: "not late"' in tile and 'word: "late"' in tile
    assert "Math.max(0, databases - lateDatabases)" in OVERVIEW
    assert "sub: <CountBar parts=" in tile


def test_databases_tile_keeps_the_server_count_behind_the_info_icon():
    tile = _tile("Databases")
    assert "info: databases ?" in tile
    assert "On {fmtInt(t.servers)}" in tile
    assert "on {fmtInt(t.servers)}" not in tile.split("info:")[0]


def test_databases_tile_says_no_databases_yet_when_there_are_none():
    assert 'empty: databases ? undefined : "No databases yet"' in _tile("Databases")


def test_freshness_legend_no_longer_repeats_the_late_databases():
    freshness = _freshness()
    assert "Late databases" not in freshness and "late_databases" not in freshness
