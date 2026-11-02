"""New job never shows the two settings only a table read by host and password uses. Edit shows them for a job with such a
table, with the reason under them, and Save sends back every stored value, shown or not. Plain source checks, no app run."""
import re
from pathlib import Path

import schedules as sch

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"
HOST_READ_REASON = "Used only by this job's tables read by host and password, not by those read through a connection."


def test_new_job_shows_neither_host_read_setting():
    call = re.search(r"<ParameterFields [^>]*/>", (PAGES / "NewJob.tsx").read_text()).group(0)
    assert "hostReads" not in call
    assert "hostReads = false" in (PAGES / "jobForm.tsx").read_text()


def test_edit_shows_them_only_when_a_table_is_read_by_host():
    assert "hostReads={!!settings.data?.reads_by_host}" in (PAGES / "JobEdit.tsx").read_text()


def test_the_two_settings_are_left_out_unless_the_job_reads_a_table_by_host():
    form = (PAGES / "jobForm.tsx").read_text()
    assert 'const HOST_READ_KEYS = ["trust_server_certificate", "secret_scope"];' in form
    assert "(hostReads || !HOST_READ_KEYS.includes(k))" in form


def test_the_reason_sits_under_them_only_when_the_job_reads_a_table_by_host():
    form = (PAGES / "jobForm.tsx").read_text()
    assert f'export const HOST_READ_REASON = "{HOST_READ_REASON}";' in form
    assert 'hostReads && k === lastHostKey && <p className="help wide">{HOST_READ_REASON}</p>' in form


def test_save_sends_back_the_stored_values_the_form_hides():
    form = (PAGES / "jobForm.tsx").read_text()
    assert "const next = { ...params };" in form
    edit = (PAGES / "JobEdit.tsx").read_text()
    assert 'parameters: Object.fromEntries(Object.entries(params).filter(([, v]) => v.trim() !== "")' in edit


def test_stored_host_read_values_pass_the_schedule_check():
    body = dict(name="Nightly", cadence_minutes=60, timezone="UTC", job_variant="default", members=["sales_1"],
                parameters={"trust_server_certificate": "true", "secret_scope": "lakefed-sql"})
    assert sch.validate(body, ["sales_1"], ["default"]) == []


def test_the_first_load_cap_is_named_for_what_it_does():
    form = (PAGES / "jobForm.tsx").read_text()
    assert "First loads per run" not in form
    assert (
        'max_seeds_per_run: ["Most tables to load for the first time in one run", "A new job\'s tables each need a first full '
        "copy. This caps how many one run does; the rest continue in the next runs, while loaded tables keep picking up "
        'changes. 0 means all in the first run."],'
    ) in form


def test_the_two_retry_settings_sit_under_a_retries_heading():
    form = (PAGES / "jobForm.tsx").read_text()
    assert 'const RETRY_KEYS = ["table_retries", "table_retry_budget"];' in form
    assert 'k === RETRY_KEYS[0] && <h4 className="wide">Retries</h4>' in form
    assert '"Retries per run", "Retries in a whole run; once spent, failures fail at once."' in form
    assert "Extra attempts at a table after a transient source error" in form
    assert "passing source error" not in form


def test_reseed_on_new_columns_sits_in_how_it_writes_with_its_reload_cost():
    form = (PAGES / "jobForm.tsx").read_text()
    assert 'const WRITE_KEYS = ["reseed_on_new_columns"];' in form
    assert "!WRITE_KEYS.includes(k)" in form
    assert (
        '"A reload copies the table again in full from the source, so a large table takes the time and source load of its '
        'first load."'
    ) in form
    assert "carries on, the new columns empty" not in form
    for name in ("NewJob.tsx", "JobEdit.tsx"):
        text = (PAGES / name).read_text()
        assert 'reseed={<ParameterField name="reseed_on_new_columns"' in text, name
