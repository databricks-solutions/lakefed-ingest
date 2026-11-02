"""Progress, the server row and toasts, as asked after a review on the deployed app: a discovery or test shows one plain
line with a spinner (the elapsed time only once it runs long) and never names a warehouse, a job or a queue; New job's
server row opens to its databases and tables through reads the app has already, with an X to remove its tables; and
an action that worked says so in a toast that fades, while errors and refusals stay as notices."""
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
PAGES = SOURCE / "pages"
COMPONENTS = (SOURCE / "components.tsx").read_text()
PENDING = (SOURCE / "pending.ts").read_text()
DISCOVER = (PAGES / "Discover.tsx").read_text()
NEW_JOB = (PAGES / "NewJob.tsx").read_text()
TREE = (PAGES / "ChosenTree.tsx").read_text()
JOB = (PAGES / "IngestionJob.tsx").read_text()
EDIT = (PAGES / "JobEdit.tsx").read_text()


def test_a_wait_is_one_plain_line_with_a_spinner():
    assert "export function Busy(" in COMPONENTS and "export function Spinner(" in COMPONENTS
    assert "export const LONG_WAIT_S = 20;" in PENDING
    for gone in ("The SQL warehouse is starting", "Waiting for compute", "reading ends", "tables: counting", "so far, since"):
        for path in [*PAGES.glob("*.ts*"), SOURCE / "pending.ts"]:
            assert gone not in path.read_text(), (gone, path.name)
    assert 'setWaiting({ asked, reading: "Testing the connection" })' in DISCOVER
    assert 'setWaiting({ asked, reading: "Reading tables" })' in DISCOVER


def test_new_jobs_server_row_opens_through_existing_reads_and_has_an_x_to_remove():
    assert "Import a file instead" not in NEW_JOB and '<th className="num">Groups</th>' not in NEW_JOB
    assert 'aria-label="Remove the tables"' in NEW_JOB and "REMOVE_CONFIRM_TABLES" in NEW_JOB
    assert "<ChosenDatabases source={chosen.source} server={cells.server} />" in NEW_JOB
    # Only the source reads the app already has; no new endpoint.
    assert "`/sources/${kind}/${encodeURIComponent(id)}/tables" in TREE and "`/sources/${kind}/${encodeURIComponent(id)}${qs(" in TREE
    assert "api.post" not in TREE


def test_an_action_that_worked_is_a_toast_and_errors_stay_notices():
    assert "export function toast(" in COMPONENTS and "export function Toaster(" in COMPONENTS
    assert "<Toaster />" in (SOURCE / "App.tsx").read_text()
    assert 'toast("Run started")' in JOB and 'toast("Cancelling the run")' in JOB and 'toast("Deleting the job")' in JOB
    assert "Started the ingestion run" not in JOB and "Databricks run\")" not in JOB
    assert 'Notice tone="warn">Not cancelled' in JOB
    assert 'toast("Groups changed")' in EDIT and "<StatusLine line={saveLine} />" in EDIT
    for page in ("Tables", "ImportCsv", "AddTable", "BulkImport", "JobEdit", "IngestionJob"):
        assert 'Notice tone="ok"' not in (PAGES / f"{page}.tsx").read_text(), page


def test_a_new_identity_column_starts_empty_and_is_saved_only_with_a_name_and_a_value():
    form = (PAGES / "jobForm.tsx").read_text()
    assert 'setIdentity([...identity, ["", ""]])' in form and 'placeholder="{database}"' not in form
    assert "<th>Column<Required /></th><th>Value<Required />" in form
    assert "identity.filter(([n, t]) => n.trim() && t.trim())" in NEW_JOB
    assert "[...typed, ...identity].filter(([n, t]) => n.trim() && t.trim())" in EDIT
