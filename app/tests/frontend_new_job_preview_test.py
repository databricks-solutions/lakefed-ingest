"""New job's preview (NewJob.tsx) as a write setting changes: the preview is asked for again under a new key, and the
tables chosen stay on screen as the last preview had them until it answers (TanStack Query's placeholderData, as the
pickers keep their pages), never a blank list. Create waits for the new answer, and tables chosen anew never show the
old ones. Plain source checks, no app run."""
from pathlib import Path

NEW_JOB = (Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages" / "NewJob.tsx").read_text()


def test_a_write_setting_change_keeps_the_chosen_tables_on_screen_until_the_new_preview_answers():
    preview = NEW_JOB[NEW_JOB.index("const preview = useQuery({"):NEW_JOB.index("// The body is the variables")]
    assert 'queryKey: ["new-job-preview", settledSource, settled]' in preview
    assert "placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === settledSource ? previous : undefined)" in preview
    assert "const settledSource = JSON.stringify(JSON.parse(settled).source ?? null);" in NEW_JOB


def test_create_waits_while_the_list_shows_the_last_preview():
    assert "preview.isPlaceholderData || settled !== current ? \"Checking the tables\"" in NEW_JOB


def test_the_list_is_drawn_from_the_preview_with_no_loading_line_of_its_own():
    # The servers come from the preview's summary; nothing spins over them while a new preview is asked for.
    assert "(Array.isArray(summary?.servers) ? summary.servers : []).map(" in NEW_JOB
    assert "preview.isFetching &&" not in NEW_JOB.replace("(preview.isFetching && preview.data === undefined)", "")
