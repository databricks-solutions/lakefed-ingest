"""Add tables from a CSV, in three steps: the connection, the file (a drop zone or a picker, with the template beside it, or
pasted text behind a toggle), then the preview (columns, counts, the lines that can't be taken with why) and the tables
chosen. Plain source checks, no app run."""
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"


def test_the_import_runs_in_three_numbered_steps():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "Step ${step} of 3: ${step === 1 ? \"the connection\" : step === 2 ? \"the file\" : \"preview and choose tables\"}" in text
    assert "useState<1 | 2 | 3>(1)" in text
    assert "setStep(3)" in text and "setStep(2)" in text


def test_the_file_step_is_a_drop_zone_with_the_template_beside_it():
    text = (PAGES / "NewJob.tsx").read_text()
    assert 'className={`drop-zone${dragging ? " over" : ""}`}' in text
    assert "onDrop={(e) =>" in text and "Drop a CSV file here" in text
    assert '<button className="ghost" onClick={() => fileInput.current?.click()}>Choose a file</button>' in text
    assert '<button className="ghost" onClick={downloadTemplate}>Download template</button>' in text


def test_pasting_is_a_toggle_and_the_raw_text_box_is_not_the_default():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "{pasting ? (" in text
    paste_branch = text.index("{pasting ? (")
    assert text.index("<textarea", paste_branch) < text.index("<div className={`drop-zone", paste_branch)
    assert '{pasting ? "Choose a file instead" : "Paste CSV instead"}' in text


def test_the_column_rules_sit_behind_an_info_tip():
    text = (PAGES / "NewJob.tsx").read_text()
    assert '<Info label="About the CSV\'s columns">One line per table or per database. Columns:' in text
    assert "The file has no" in text and "connection column" in text


def test_the_preview_gives_columns_counts_and_lines_that_cannot_be_taken_with_why():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "Columns found:" in text
    assert "(discovered at Create), {fmtInt(invalid)} {plural(invalid, \"line\")} that can't be taken." in text
    assert 'line {l.line}{l.table ? `, ${l.table}` : ""}: {l.errors.join("; ")}' in text
    assert "Showing the first" in text


def test_the_preview_shows_the_first_lines_under_the_header_from_the_upload_read_back():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "const PREVIEW_LINES = 5;" in text
    assert "`/uploads/${upload!.upload_id}/lines?limit=${PREVIEW_LINES}`" in text
    assert "The first {fmtInt(firstLines.data!.lines.length)} of {fmtInt(Number(upload.lines))} lines:" in text
    assert '<thead><tr><th className="num">Line</th>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>' in text


def test_the_connection_step_keeps_how_its_tables_load():
    text = (PAGES / "NewJob.tsx").read_text()
    assert "<label>How its tables load<select value={load}" in text
    assert "Upload a different file" in text
