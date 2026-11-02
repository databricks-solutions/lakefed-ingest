"""The Overview page's Rows written tile, as the cards board approved it: one headline number with no bar, and the
"by every job's runs" line moved behind the info icon, where the partial-count note also sits. Plain source checks on
the TSX, as the other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _tile(label):
    start = OVERVIEW.index(f'label: "{label}"')
    return OVERVIEW[start:OVERVIEW.index("\n  ] : [];", start)]


def test_rows_tile_is_one_number_with_no_bar_and_no_sub_line():
    tile = _tile("Rows written, 24 hours")
    assert "sub:" not in tile and "CountBar" not in tile
    assert "at least " in tile


def test_rows_tile_keeps_the_job_runs_meaning_and_the_partial_note_behind_the_info_icon():
    tile = _tile("Rows written, 24 hours")
    assert "info: <>Rows every job's ingestion runs wrote in the last 24 hours." in tile
    assert 'means some loads did not record a count.' in tile
    assert "d.rows_24h.partial &&" in tile.split("info:")[1]
