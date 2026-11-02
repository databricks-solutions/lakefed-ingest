"""The Discover step's database list through the SQL warehouse (Discover.tsx): each database checked, the ones the
login can't open greyed out with why on hover and never chosen, and the choice counted at the top and on Add."""
from pathlib import Path

DISCOVER = (Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages" / "Discover.tsx").read_text()
STYLES = (Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "styles.css").read_text()


def test_the_listed_databases_are_checked_the_page_shown_first():
    assert "/access${qs({ connection })}" in DISCOVER and "first: shownKey" in DISCOVER
    assert "checksOn={throughWarehouse}" in DISCOVER and 'tested?.via === "warehouse"' in DISCOVER


def test_a_database_the_login_cant_open_is_greyed_out_says_why_on_hover_and_is_never_chosen():
    assert 'closed(d) ? " closed" : ""' in DISCOVER and "can't open: hover for why" in DISCOVER
    assert "<Tip text={String(seen[d]?.why" in DISCOVER
    assert 'names.filter((d) => pick === "none" || !closed(d))' in DISCOVER
    assert "coveredByServer(wholeServer, d) && !closed(d)" in DISCOVER
    assert "tr.closed .tri-box { opacity: .35; pointer-events: none; }" in STYLES


def test_the_choice_is_counted_at_the_top_on_add_and_in_the_jobs_chosen_summary():
    assert '<p className="pick-counts"><b>{countsText(counts, total)}</b>' in DISCOVER
    assert "the whole server leaves out" in DISCOVER
    assert "{chosenWords} chosen. {countsLine}." in DISCOVER
    assert 'countsLine.replace(/^Chosen: /, "")' in DISCOVER


def test_the_whole_server_is_read_from_the_tests_listing_through_the_warehouse():
    assert "{ whole_server: true, listing_id: discoveryId, filter: wholeServer.filter }" in DISCOVER
    assert "readTables(connection, request, onProgress)" in DISCOVER
