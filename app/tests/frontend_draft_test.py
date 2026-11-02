"""New job's drafts (Discover.tsx, NewJob.tsx, tasks.tsx): the tables of what is chosen whole are read as it is
chosen, Add hands the reads over still going, New job keeps them going through its preview, Create waits for them,
leaving the page or choosing again before Create discards them, and a database un-ticked leaves its draft. Edit's Add
tables keeps waiting at Add."""
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"
DISCOVER = (PAGES / "Discover.tsx").read_text()
NEW_JOB = (PAGES / "NewJob.tsx").read_text()
JOB_EDIT = (PAGES / "JobEdit.tsx").read_text()
TASKS = (PAGES / "tasks.tsx").read_text()


def test_a_choice_starts_its_draft_and_grows_it_as_more_is_chosen():
    assert "draft_id: d.named" in DISCOVER and "draft: true" in DISCOVER
    assert "whole_server: true,\n" in DISCOVER and "listing_id: discoveryId, filter: want, draft: true" in DISCOVER
    assert "if (drafting) syncDrafts();" in DISCOVER


def test_add_hands_the_drafts_over_without_waiting_and_only_new_job_asks_for_that():
    assert "onClose({ source, label: label(), connection, drafts: allDrafts() });" in DISCOVER
    assert "<DiscoverDialog drafts " in NEW_JOB and "<DiscoverDialog drafts" not in JOB_EDIT


def test_leaving_or_choosing_again_discards_the_drafts_unless_a_create_took_them():
    assert "navigator.sendBeacon(`/api${path}`)" in DISCOVER and 'addEventListener("pagehide", leave)' in NEW_JOB
    assert "if (!created.current) discardDrafts(drafts.current);" in NEW_JOB
    assert "created.current = true;" in NEW_JOB


def test_new_job_keeps_the_read_going_and_says_how_far_it_is():
    assert "query.state.data?.reading?.pending ? 2000" in NEW_JOB
    assert "draftReadProgress(reading)" in NEW_JOB and "draftCreateBlock(reading)" in NEW_JOB
    assert '<Busy label="Reading tables" {...readProgress} />' in NEW_JOB
    assert 'progress?.stage === "reading"' in TASKS


def test_reading_tables_shows_only_after_add_while_the_reads_still_start_as_the_choice_is_made():
    # While choosing (step 2) the drafts' reads go on out of sight: no Reading tables line or bar in the dialog for them.
    assert "useDraftReads(connection, draftIds, draftGeneration);" in DISCOVER
    assert "useEffect(() => { if (drafting) syncDrafts(); }, [drafting, wholeKey, wholeServer.on, wholeServer.filter]);" in DISCOVER
    assert "draftProgress" not in DISCOVER and "step === 2 && drafting" not in DISCOVER
    # The dialog's only Busy line is its wait (the connection test, or the reading after Add on Edit's Add tables), and
    # New job shows the drafts' progress once Add has handed them over.
    assert DISCOVER.count("<Busy ") == 1
    assert "{waiting && <Busy label={waiting.reading} since={waiting.asked} done={waiting.done} total={waiting.total} />}" in DISCOVER
    assert 'setWaiting({ asked, reading: "Reading tables" });' in DISCOVER[DISCOVER.index("const finish = async () => {"):]
    assert '{readProgress && <Busy label="Reading tables" {...readProgress} />}' in NEW_JOB


def test_a_database_un_ticked_is_dropped_from_its_draft_and_comes_back_when_ticked_again():
    sync = DISCOVER[DISCOVER.index("const syncDrafts = () => {"):DISCOVER.index("const wholeKey = whole.join")]
    assert "const unticked = [...d.names].filter((name) => !wholeNow.includes(name));" in sync
    assert "await api.post(`/discoveries/${encodeURIComponent(d.named)}/drop`, { databases: unticked });" in sync
    assert "unticked.forEach((name) => d.names.delete(name));" in sync
    # Dropped before the draft grows, so a database ticked again is named again (warehouse_named_rows_sql takes it back).
    assert sync.index("/drop`") < sync.index("const fresh = wholeNow.filter((name) => !d.names.has(name) && !readNow[name]);")
    assert sync.index("const fresh") < sync.index("draft_id: d.named")
