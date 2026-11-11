"""The pages for a job that isn't Ready yet: the job page's Preparing card (its line and bar, Run now and the schedule
available when the job is ready, Needs you with the set-aside databases and their actions), Edit's locks, the jobs
list's status (from the server), and New job opening the job page once Create names it. Plain source checks, as the
other frontend tests are, and the Node tests of the card's helpers (app/tests/js/preparing.test.mjs), skipped where
Node is not installed."""
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
SOURCE = APP / "src" / "frontend" / "src"
PAGES = SOURCE / "pages"
JOB = (PAGES / "IngestionJob.tsx").read_text()
EDIT = (PAGES / "JobEdit.tsx").read_text()
FORM = (PAGES / "jobForm.tsx").read_text()
NEW_JOB = (PAGES / "NewJob.tsx").read_text()
HELPERS = (SOURCE / "preparing.ts").read_text()


def _function(source, name):
    start = source.index(f"function {name}(")
    return source[start:source.index("\n}\n", start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_preparing_helpers_pass_their_node_tests():
    tests = APP / "tests" / "js" / "preparing.test.mjs"
    result = subprocess.run(["node", "--test", str(tests)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_job_page_shows_the_preparing_card_until_the_job_is_ready():
    assert ('{j?.prepare && j.prepare.state !== "ready" && !j.delete\n'
            '        && <Preparation jobId={id} deleteRefusal={j.delete_blocked} onDelete={() => setDeleting(true)} />}') in JOB
    card = _function(JOB, "Preparation")
    assert "`/ingestion-jobs/${encodeURIComponent(jobId)}/prepare`" in card
    assert '=== "preparing" ? 3_000 : 15_000' in card
    assert "<Busy label={line} done={bar?.done} total={bar?.total} />" in card
    assert "<p className=\"muted small\">Run now: {AVAILABLE_WHEN_READY}. {scheduleWhenReady(p)}.</p>" in card
    assert 'export const AVAILABLE_WHEN_READY = "available when the job is ready";' in HELPERS
    assert '<Badge value={j.prepare.state === "needs_you" ? "Needs you" : "Preparing"} />' in JOB


def test_needs_you_lists_the_set_aside_databases_and_offers_the_four_actions():
    card = _function(JOB, "Preparation")
    assert "<th>Why it was set aside</th>" in card and "{setAsideWhy(e)}" in card
    for label, action in (("Read them again", "read_again"), ("Leave them out", "leave_out"),
                          ("Change the job's target names", "targets")):
        assert f">{label}</button>" in card and f"title={{actions.{action} ?? undefined}}" in card
    assert 'onClick={onDelete}>Delete the job</button>' in card
    assert 'act.mutate({ action: "read-again", body: {} })' in card and 'act.mutate({ action: "leave-out", body: {} })' in card
    assert 'title: "Leave them out?"' in card
    names = _function(JOB, "TargetNames")
    assert "/prepare/targets`" in names and "{ ...names, dry_run: dryRun }" in names
    assert 'actionDisabled={!check.data ? "Check the names first" : undefined}' in names


def test_preparing_offers_stop_preparing_and_a_stopped_job_prepare_again_or_delete():
    card = _function(JOB, "Preparation")
    assert 'title: "Stop preparing?", action: "Stop preparing"' in card
    assert 'act.mutate({ action: "stop", body: {} })' in card and 'act.mutate({ action: "again", body: {} })' in card
    assert "onClick={stopPreparing}>Stop preparing</button>" in card and ">Prepare again</button>" in card
    assert "title={actions.stop ?? undefined}" in card and "title={actions.again ?? undefined}" in card
    assert '{p.state === "preparing" && !stopped && <div className="toolbar">' in card
    assert '<Section title={stopped ? "Stopped" : p.state === "needs_you" ? "Needs you" : "Preparing"}' in card
    assert "if (view.stopped) return `Stopped: ${view.stopped.why" in HELPERS


def test_edit_keeps_what_waits_for_ready_locked_and_says_why():
    assert "const notReady = !!schedule?.options?.prepare?.state && schedule.options.prepare.state !== \"ready\";" in EDIT
    assert "<Tree jobId={id} locked={locked ?? tablesLocked} />" in EDIT
    assert "<Groups jobId={id} locked={locked ?? settingsLocked}" in EDIT
    assert EDIT.count('<fieldset className="create-lock" disabled={!!settingsLocked}') == 2
    assert "pausedLocked={notReady ? AVAILABLE_WHEN_READY : null}" in EDIT
    run_fields = FORM[FORM.index("export function RunFields("):]
    assert "disabled={!!pausedLocked}" in run_fields and "{pausedLocked && <span className=\"muted small\"> ({pausedLocked})</span>}" in run_fields


def test_new_job_opens_the_job_page_once_create_names_the_job():
    assert ("if (r.id != null && (r.created || r.prepare)) navigate(`/ingestion-jobs/${r.id}`, "
            "{ state: { warnings: r.warnings ?? [] } });") in NEW_JOB


def test_the_jobs_list_takes_its_status_from_the_server():
    jobs_module = (APP / "src" / "ingestion_jobs.py").read_text()
    assert 'row["status"] = preparing.status_cell(options) or row["status"]' in jobs_module
    assert "<td><JobStatus status={j.status} /></td>" in (PAGES / "Overview.tsx").read_text()
