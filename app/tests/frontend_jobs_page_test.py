"""The Ingestion jobs page and the shell, as tidied after a review on the deployed app: tiles that open something
tint as a whole on hover (no underlined text), the job list has its own section header and no colour key, New
ingestion job sits with the filters, and the "Updated" line and Refresh data are at the bottom of the left menu."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
STYLES = (SOURCE / "styles.css").read_text()
JOBS = (SOURCE / "pages" / "IngestionJobs.tsx").read_text()
APP = (SOURCE / "App.tsx").read_text()


def _rule(selector):
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLES)
    assert match, selector
    return match.group(1)


def test_a_clickable_tile_tints_on_hover_and_never_underlines():
    hover = _rule(".metric-link:hover")
    assert "text-decoration: none" in hover and "background:" in hover
    assert "cursor: pointer" in _rule(".metric-link")
    assert ".metric:hover" not in STYLES                  # a tile that opens nothing has no hover


def test_the_job_list_has_a_header_and_no_colour_key():
    assert '<Section title="Jobs">' in JOBS
    assert "StateKey" not in JOBS


def test_the_jobs_table_bars_count_n_of_n_loaded_and_never_say_all_n_loaded():
    overview = (PAGES / "Overview.tsx").read_text()
    assert overview.count("<StateBar row={j} saysAllDone={false} />") == 1
    assert overview.count("<StateBar row={notInJob} saysAllDone={false} />") == 1
    assert "<StateBar row={j} />" not in overview


def test_new_ingestion_job_is_on_the_filter_row_at_its_end():
    toolbar = JOBS[JOBS.index('<div className="toolbar">'):JOBS.index('<Section title="Jobs">')]
    assert 'className="primary toolbar-end"' in toolbar and "New ingestion job" in toolbar
    assert "margin-left: auto" in _rule(".toolbar .toolbar-end")
    assert "right=" not in JOBS[JOBS.index("<PageHead"):JOBS.index("/>", JOBS.index("<PageHead"))]


def test_updated_and_refresh_data_are_at_the_bottom_of_the_menu():
    foot = APP[APP.index('<div className="sidebar-foot">'):APP.index("</aside>")]
    assert "<Stamp" in foot and "Refresh data" in foot
    top = APP.index('<div className="topbar">')
    assert "Refresh data" not in APP[top:APP.index("</div>", top)]
    assert "margin-top: auto" in _rule(".sidebar-foot")
    for page in ("IngestionJobs", "Overview", "Sources", "Schedules", "Changes", "Timeline"):
        text = (SOURCE / "pages" / f"{page}.tsx").read_text()
        assert "usePageStamp(" in text and "right={<Stamp" not in text, page


def test_a_new_jobs_name_is_its_title_edited_in_place_with_a_default():
    page = (SOURCE / "pages" / "NewJob.tsx").read_text()
    assert 'useState(defaultJobName)' in page and 'className="title-edit" aria-label="Job name"' in page
    assert '<Section title="Name">' not in page and "e.g. Engagement extraction" not in page
    assert "named after it" not in page                     # the groups are, behind the scenes, with no word of it


PAGES = SOURCE / "pages"
FORM = (PAGES / "jobForm.tsx").read_text()
NEW_JOB = (PAGES / "NewJob.tsx").read_text()
JOB_EDIT = (PAGES / "JobEdit.tsx").read_text()


def test_required_fields_have_an_asterisk_and_no_needed_note():
    for text in (FORM, NEW_JOB, JOB_EDIT):
        assert "Needed to create the job" not in text
    assert FORM.count("<Required />") >= 3 and "<Required />" in JOB_EDIT
    assert ".required" in STYLES


def test_explanations_sit_behind_an_info_icon():
    assert "export function Info(" in (SOURCE / "components.tsx").read_text()
    assert '<Info label="About fan-in">' in FORM
    assert ".info-tip:hover .info-pop" in STYLES or ".info-tip:hover" in STYLES
    discover = (PAGES / "Discover.tsx").read_text()
    assert "<Info>Next tests it" in discover and "Discover is the first choice" in discover


def test_the_form_grid_is_fluid_with_at_most_four_columns():
    grid = _rule(".form-grid")
    assert "auto-fill" in grid and "minmax(" in grid and "/ 4" in grid


def test_new_job_drops_runs_start_and_the_advanced_summary_and_sends_the_check_variant():
    assert "<label>Runs start" not in NEW_JOB and "setVariant" not in NEW_JOB
    assert "paramsSummary" not in NEW_JOB and "clusterSummary" not in NEW_JOB
    assert 'variants.includes("check") ? "check" : "default"' in NEW_JOB


def test_tables_per_group_is_sent_only_when_changed_and_groups_per_server_never():
    assert "...(tablesPerGroup ? { tables_per_group: tablesPerGroup } : {})" in NEW_JOB
    assert "...(size ? { tables_per_group: size } : {})" in JOB_EDIT
    assert "DEFAULT_TABLES_PER_GROUP = 10_000" in JOB_EDIT
    for text in (NEW_JOB, JOB_EDIT):
        assert "groups_per_server" not in text


def test_tuning_shows_the_defaults_saves_unchanged_as_the_default_and_can_reset():
    assert "params[k] ?? defaults" in FORM or "?? defaults[" in FORM
    assert 'className="ghost link reset"' in FORM and '<input type="checkbox" checked={value === "true"}' in FORM


def test_deleted_jobs_are_folded_counted_and_paged():
    assert "Deleted jobs (" in JOBS and "DELETED_PAGE" in JOBS and "<Pager" in JOBS
    assert "rows: open" in JOBS
    for words in ("Restore", "Delete now"):             # a kept deleted job is restored or purged from this section
        assert words in JOBS, words


def test_the_menu_is_one_flat_list():
    assert "const NAV: [string, string][]" in APP
    assert "nav-group" not in APP and '"Monitor"' not in APP and '"Manage"' not in APP


def test_the_tables_page_filters_and_shows_the_ingestion_job():
    tables = (PAGES / "Tables.tsx").read_text()
    assert 'ingestion_job: { headerName: "Ingestion job" }' in tables and 'params.get("job")' in tables


def test_a_run_with_failed_tables_fails_with_no_box_and_the_page_never_sets_the_parameter():
    assert "Also email when tables fail" not in FORM
    for text in (FORM, NEW_JOB, JOB_EDIT):
        assert "fail_on_table_errors:" not in text and "fail_on_table_errors =" not in text
    assert '.filter((k: string) => k !== "fail_on_table_errors"' in FORM      # not a tuning field either
