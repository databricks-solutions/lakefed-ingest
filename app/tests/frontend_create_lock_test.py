"""While Create runs, New job is read only: the title, every field, Add tables and Cancel. Create cannot be cancelled, so
Cancel is off too. Plain source checks, no app run."""
import re
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"


def test_the_form_below_the_title_is_one_disabled_fieldset_while_create_runs():
    text = (PAGES / "NewJob.tsx").read_text()
    opening = text.index('<fieldset className="create-lock" disabled={create.isPending}>')
    closing = text.index("</fieldset>", opening)
    body = text[opening:closing]
    assert '<Section title="What it ingests"' in body
    assert "<ParameterFields" in body and "<WritesFields" in body and "<IdentityFields" in body
    assert "<TaskProgress" not in body


def test_the_title_and_cancel_are_read_only_while_create_runs():
    text = (PAGES / "NewJob.tsx").read_text()
    title = re.search(r'<input className="title-edit" aria-label="Job name"[^>]*/?>', text, re.S).group(0)
    assert "readOnly={create.isPending}" in title
    assert "<button onClick={cancel} disabled={create.isPending}>Cancel</button>" in text


def test_the_fieldset_resets_the_browser_frame():
    css = (PAGES.parent / "styles.css").read_text()
    assert ".create-lock { border: 0; margin: 0; padding: 0; min-width: 0; }" in css
