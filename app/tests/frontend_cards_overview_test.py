"""The Overview page's headline tiles, as the cards board approved them. Plain source checks on the TSX and CSS, as the
other frontend tests are."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
COMPONENTS = (SOURCE / "components.tsx").read_text()
STYLES = (SOURCE / "styles.css").read_text()
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()


def _function(source, name):
    start = source.index(f"export function {name}(")
    return source[start:source.index("\nexport ", start + 1)]


def _tile(label):
    start = OVERVIEW.index(f'label: "{label}"')
    return OVERVIEW[start:OVERVIEW.index('{ label: "', start + 1)]


def test_count_bar_draws_one_bar_and_a_legend_of_the_counts_that_are_not_zero():
    count_bar = _function(COMPONENTS, "CountBar")
    assert "<Segments parts={parts} total={total} />" in count_bar
    assert "legend && parts.filter((part) => part.count > 0)" in count_bar


def test_state_summary_uses_the_shared_count_bar():
    summary = _function(COMPONENTS, "StateSummary")
    assert "<CountBar parts={parts}" in summary
    assert "<Segments" not in summary


def test_ingestion_jobs_tile_is_a_bar_of_running_paused_and_idle_jobs():
    tile = _tile("Ingestion jobs")
    assert 'to: "/ingestion-jobs"' in tile
    assert 'word: "running"' in tile and 'word: "paused"' in tile and 'word: "idle"' in tile
    assert "Math.max(0, jobs - running - paused)" in OVERVIEW
    assert "sub: <CountBar parts=" in tile


def test_ingestion_jobs_tile_says_no_ingestion_jobs_yet_when_there_are_none():
    assert 'empty: jobs ? undefined : "No ingestion jobs yet"' in _tile("Ingestion jobs")


def test_groups_in_no_job_sit_behind_the_info_icon_not_in_the_sub_line():
    tile = _tile("Ingestion jobs")
    assert "info: unscheduled ?" in tile
    assert "groups in no job" not in OVERVIEW
    assert "<TileInfo>{tile.info}</TileInfo>" in OVERVIEW


def test_an_empty_tile_shows_its_text_in_the_small_muted_value_style():
    assert '<div className="metric-value small-value muted">{tile.empty}</div>' in OVERVIEW
    assert ".metric-value.small-value" in STYLES


def test_a_tile_info_icon_stops_click_enter_and_space_before_the_tile_link():
    tile_info = OVERVIEW[OVERVIEW.index("function TileInfo("):OVERVIEW.index("export default function OverviewPage")]
    assert "event.preventDefault();" in tile_info
    assert "event.stopPropagation();" in tile_info
    assert "<span onClick={keepOnPage} onKeyDown={(event) => { if (event.key === \"Enter\" || event.key === \" \") keepOnPage(event); }}>" in tile_info
    assert "<Info>{children}</Info>" in tile_info


def test_every_tile_renders_its_info_through_the_stopping_wrapper():
    assert 'className="metric metric-link"' in OVERVIEW
    assert "<div className=\"metric-label\">{tile.label}{tile.info && <TileInfo>{tile.info}</TileInfo>}</div>" in OVERVIEW
    assert "<Info>{tile.info}</Info>" not in OVERVIEW
