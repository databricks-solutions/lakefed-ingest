"""The Identity columns section (jobForm.tsx's IdentityFields, shown by New job and the job's Edit): the __client_id__
row shows only with fan-in on (or a fan-in setting that differs between tables), never with fan-in off. Only what is
shown changes, not what is sent. Plain source checks, no app run."""
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"
FORM = (PAGES / "jobForm.tsx").read_text()


def test_the_client_id_row_is_left_out_with_fan_in_off_in_new_job_and_edit():
    fields = FORM[FORM.index("export function IdentityFields("):FORM.index("export function RunFields(")]
    # One gate before both rows: New job's read-only key and Edit's typed one.
    assert "{w.fan_in === false ? null : newJob ? (" in fields
    assert fields.count("__client_id__</td>") == 1 and fields.count('<td className="mono">__client_id__') == 2
    # With fan-in on, as before: New job names the server's key, Edit types it.
    assert "filled from the server and database: <span className=\"mono\">{sharedKey}</span>" in fields
    assert '<input className="mono" value={w.client_id ?? ""}' in fields and "disabled={w.fan_in === false}" not in fields


def test_both_pages_draw_the_identity_columns_with_the_shared_fields():
    for page in ("NewJob.tsx", "JobEdit.tsx"):
        assert "<IdentityFields " in (PAGES / page).read_text(), page
