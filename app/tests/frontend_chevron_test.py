"""Every row or section that opens uses the Databricks UI's chevron (components.Chevron, Twisty), never a text
triangle, and Running now lists a job run in the order it goes."""
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src"
FILES = sorted(SOURCE.rglob("*.ts*"))
TRIANGLES = re.compile("[▶▸▹►▾▿▼◂◀]")


def test_no_text_triangle_opens_anything():
    # The Timeline's skipped-trigger mark is a mark on a chart, not a toggle.
    found = [f"{path.name}:{number}" for path in FILES for number, line in enumerate(path.read_text().splitlines(), 1)
             if TRIANGLES.search(line) and not (path.name == "Timeline.tsx" and ("skip" in line.lower() or "fill" in line))]
    assert found == []


def test_rows_that_open_use_the_shared_twisty():
    for name in ("Discover.tsx", "SourcePick.tsx", "JobEdit.tsx", "IngestionJob.tsx", "TargetTables.tsx"):
        text = (SOURCE / "pages" / name).read_text()
        assert "<Twisty" in text, name
        assert 'className="twisty"' not in text, name


def test_the_twisty_is_a_button_that_says_whether_it_is_open():
    components = (SOURCE / "components.tsx").read_text()
    twisty = re.search(r"export function Twisty\b.*?\n}\n", components, re.S).group(0)
    assert "<button" in twisty and "aria-expanded={closed ? undefined : open}" in twisty and 'type="button"' in twisty


def test_a_row_that_cannot_open_has_its_chevron_off_with_the_reason():
    components = (SOURCE / "components.tsx").read_text()
    twisty = re.search(r"export function Twisty\b.*?\n}\n", components, re.S).group(0)
    assert "disabled={!!closed}" in twisty and "title={closed ?? label}" in twisty
    assert "onClick={closed ? undefined : onToggle}" in twisty
    discover = (SOURCE / "pages" / "Discover.tsx").read_text()
    assert "closed={closed(d) ?" in discover                  # a greyed database's chevron is off, saying why
    assert "if (!readable([d], seen).length) return;" in discover   # and opening it reads nothing


def test_running_now_lists_the_check_before_the_group_runs():
    page = (SOURCE / "pages" / "IngestionJob.tsx").read_text()
    running = re.search(r"function RunningNow\b.*?\n}\n", page, re.S).group(0)
    assert running.index("The job's run") < running.index(">1</span>Check") < running.index(">2</span>}Group runs")
    assert "Start when the check ends." in running
