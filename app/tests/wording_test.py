"""The app's words: what the control table calls a task_collection, the app shows as an "ingestion group".

The column, the job parameters and the API keep their names; only text a person reads changes. This scans the
frontend's visible text (JSX text, and the strings of labels, titles, notes, column headers and confirmations) and
the backend's messages for the old word."""
import re
from pathlib import Path

import schedules as sch

FRONTEND = Path(__file__).resolve().parent.parent / "src" / "frontend" / "src"
OLD_WORD = re.compile(r"\bcollections?\b", re.IGNORECASE)
COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
JSX_TEXT = re.compile(r">([^<>{}]+)<")
VISIBLE_STRINGS = re.compile(
    r"\b(?:label|title|note|headerName|confirmLabel|aria-label|placeholder|crumb)\s*[=:]\s*[{(]?\s*[\"`]([^\"`]*)[\"`]")
STRING_LITERALS = re.compile(r"\"([^\"\n]*)\"|`([^`\n]*)`")


def visible_text(source: str) -> list[str]:
    code = COMMENTS.sub("", source)
    # Text between tags, but not TypeScript between generics' angle brackets (it carries code punctuation).
    found = [m.group(1) for m in JSX_TEXT.finditer(code) if not re.search(r"[;=()\[\]]", m.group(1))]
    found += [m.group(1) for m in VISIBLE_STRINGS.finditer(code)]
    # Any other literal with a space in it reads as a phrase (message, notice); paths and keys have none.
    found += [s for m in STRING_LITERALS.finditer(code) for s in m.groups() if s and " " in s and not s.startswith("/")]
    return found


def test_no_page_shows_the_word_collection():
    offenders = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        for text in visible_text(path.read_text()):
            cleaned = re.sub(r"\$\{[^}]*\}|\btask_collection\b|\bTAG_COLLECTION\b", "", text)
            if OLD_WORD.search(cleaned):
                offenders.append(f"{path.name}: {text.strip()[:80]}")
    assert not offenders, "use 'ingestion group' in the app's wording:\n" + "\n".join(offenders)


def test_the_check_catches_the_old_word():
    sample = '<th>Collection</th> <Section title="Collections" /> setMessage(`Refused while the collection runs`)'
    assert len([t for t in visible_text(sample) if OLD_WORD.search(t)]) == 3


def test_schedule_messages_say_ingestion_group():
    errors = " ".join(sch.validate(dict(name="s", cadence_minutes=60, timezone="UTC", members=["a", "a", "zz"]),
                                   ["a"], ["default"]))
    assert "ingestion group" in errors and not OLD_WORD.search(errors.replace("members:", ""))


# "Ingestion job" means a schedule now; the bundle-deployed job is "the Databricks job".
DATABRICKS_JOB_AS_INGESTION_JOB = re.compile(r"\bthe ingestion job\b", re.IGNORECASE)


def test_no_page_calls_the_databricks_job_an_ingestion_job():
    offenders = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        for text in visible_text(path.read_text()):
            if DATABRICKS_JOB_AS_INGESTION_JOB.search(text):
                offenders.append(f"{path.name}: {text.strip()[:80]}")
    new_job = (FRONTEND / "pages" / "NewJob.tsx").read_text()
    if re.search(r"<label>Ingestion job\b", new_job):
        offenders.append("NewJob.tsx: the variant select is labelled Ingestion job")
    assert not offenders, "say 'the Databricks job' for the bundle-deployed job:\n" + "\n".join(offenders)


def test_the_nav_says_schedules_and_its_page_shows_only_the_schedules():
    app = (FRONTEND / "App.tsx").read_text()
    assert '["/schedules", "Schedules"]' in app and '["/jobs", "Jobs"]' not in app
    assert '<Route path="/schedules-and-jobs" element={<Navigate to="/schedules" replace />} />' in app
    page = (FRONTEND / "pages" / "Schedules.tsx").read_text()
    assert '<PageHead title="Schedules"' in page and "<SchemaSetup />" not in page
    for gone in ("The Databricks jobs the app runs them with", "Active runs", "Recent runs", "<Jobs"):
        assert gone not in page, gone
    assert not (FRONTEND / "pages" / "Jobs.tsx").exists()


def test_one_trigger_of_an_ingestion_job_is_an_ingestion_run():
    # "a Databricks job run" stays, and "each ingestion job runs" is the verb.
    offenders = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        for text in visible_text(path.read_text()):
            if re.search(r"(?<!Databricks )(?<!discovery )(?<!ingestion )\b[Jj]ob runs?\b", text):
                offenders.append(f"{path.name}: {text.strip()[:80]}")
    assert not offenders, "say 'ingestion run' for one trigger of an ingestion job:\n" + "\n".join(offenders)



# The app shows ingestion jobs and ingestion runs; a Databricks job's title (prefixed in a development deploy) is
# under-the-hood detail. Manage's list of the Databricks jobs, for managers only, is the one place that names them.
DATABRICKS_JOB_LABELS = re.compile(r"Databricks job run|Open in Jobs|lakefed_ingest|\[dev |\bjob \d")


def test_no_page_renders_a_databricks_job_name():
    offenders = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        if path.name.startswith("Manage"):
            continue
        source = path.read_text()
        offenders += [f"{path.name}: {text.strip()[:80]}" for text in visible_text(source)
                      if DATABRICKS_JOB_LABELS.search(text)]
        # The Databricks job's summary (its title), the Compute block's job names, and links labelled with a job or
        # a Databricks job run.
        offenders += [f"{path.name}: {shown}" for shown in ('"/jobs/ingest"', "job.name ?? job.job_id", ">job {",
                                                            ">Databricks job run") if shown in source]
    assert not offenders, "name the ingestion job or run, not the Databricks job:\n" + "\n".join(offenders)


def test_the_backend_names_the_databricks_job():
    main_py = (FRONTEND.parent.parent / "main.py").read_text()
    assert "no ingestion job configured" not in main_py and "no Databricks ingestion job configured" in main_py


def test_discover_says_when_the_login_cant_open_master_and_takes_typed_databases():
    discover = (FRONTEND / "pages" / "Discover.tsx").read_text()
    assert "If the login can't open master" in discover and "VIEW ANY" in discover
    assert "function TypedDatabases" in discover and "Databases typed by hand are read by name" in discover


def test_discover_lists_every_database_then_filters_and_says_which_databases_it_couldnt_read():
    discover = (FRONTEND / "pages" / "Discover.tsx").read_text()
    assert "Databases matching" not in discover and "listing every database on the server" in discover
    assert "function Unread" in discover and "couldn't be read" in discover
    assert "databasesOfCsv" not in discover and "CSV file" not in discover
    assert "Discover is the first choice" in discover and "the fallback" in discover


def test_next_on_the_connection_step_tests_it_with_no_button_of_its_own():
    discover = (FRONTEND / "pages" / "Discover.tsx").read_text()
    assert "Test connection" not in discover and "testThenNext" in discover and "Next tests it" in discover
    assert "<TestFailed" in discover and "actionNote=" in discover


def test_discover_can_list_an_azure_sql_server_through_azure_and_reads_the_whole_server_the_same_way():
    discover = (FRONTEND / "pages" / "Discover.tsx").read_text()
    assert "Service credential" in discover and "Server resource id" in discover and "Reader on the server" in discover
    assert "azure_credential" in discover and "azure_resource_id" in discover and "...(tested?.azure ?? {})" in discover


def test_the_target_fields_are_marked_required_with_no_help_under_them():
    # The schema's how-it-is-made note is gone (his review); the fields carry the required mark instead.
    form = (FRONTEND / "pages" / "jobForm.tsx").read_text()
    assert "A new schema is made by the first run" not in form and "Needed to create the job" not in form
    assert form.count("<Required />") >= 3


def test_discover_offers_typed_or_csv_databases_on_the_lab_logins_real_errors():
    import re
    discover = (FRONTEND / "pages" / "Discover.tsx").read_text()
    source = re.search(r"const cantList = .*?/(.+)/i\.test", discover).group(1)
    cant_list = re.compile(source, re.I)
    # lfc_sqlserver_test's login, a user contained in each database, opening master (29 Oct):
    assert cant_list.search("[FAILED_JDBC.CONNECTION] Failed JDBC on the operation: Failed to connect to the database. "
                            "SQLSTATE: HV000")
    assert cant_list.search("Login failed for user 'lfc_ingest'.")
    assert not cant_list.search("the discovery run ended CANCELED")


def test_delete_says_what_goes_and_what_stays_and_offers_the_target_tables():
    page = (FRONTEND / "pages" / "IngestionJob.tsx").read_text()
    assert "Also delete the target tables" in page and "drop_targets: dropTargets" in page
    assert "with their checkpoints. Run history and the change log stay." in page
    assert "another job also writes" in page and "kept_names" in page and "schemas stay" in page
    assert "move to Not in any job" not in page                    # groups always belong to a job: none are left behind
    assert "Deleting…" in page                                     # the Delete job button while the delete is started
    # A failed delete is run again from its page.
    assert "Delete now runs it again." in (FRONTEND / "pending.ts").read_text()
    jobs_page = (FRONTEND / "pages" / "IngestionJobs.tsx").read_text()
    assert "Deleted jobs" in jobs_page and "again.mutate" not in jobs_page
