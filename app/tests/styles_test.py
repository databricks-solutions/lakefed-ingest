"""The state bar's colours (styles.css): each state reads as a solid part of one bar."""
import re
from pathlib import Path

STYLES = (Path(__file__).resolve().parent.parent / "src" / "frontend" / "src" / "styles.css").read_text()


def rule(selector: str) -> str:
    """The declarations of the first rule whose selector list contains `selector`."""
    match = re.search(r"(?:^|[},])\s*[^{}]*" + re.escape(selector) + r"\b[^{}]*\{([^}]*)\}", STYLES)
    assert match, selector
    return match.group(1)


def test_waiting_for_a_first_load_is_a_solid_grey_with_no_outline():
    assert "box-shadow" not in rule(".seg-waiting")
    assert "border" not in rule(".seg-waiting")
    waiting = re.search(r"--s-waiting:\s*([^;]+);", STYLES).group(1)
    assert waiting.replace(" ", "") == "color-mix(insrgb,var(--db-text-subtle)55%,white)"


def test_the_states_take_the_databricks_status_colours():
    """Green loaded, blue running, grey waiting, amber late, red failed: the design system's status tokens."""
    colours = dict(re.findall(r"--s-([a-z]+):\s*([^;]+);", STYLES))
    assert colours["loaded"] == "var(--db-positive-border)"
    assert colours["running"] == "var(--db-accent-surface)"
    assert colours["late"] == "var(--db-caution-surface-strong)"
    assert colours["failing"] == "var(--db-negative-surface)"
    for tone, token in (("loaded", "--ok"), ("late", "--warn"), ("failing", "--bad")):
        assert f"var({token})" in rule(f".tone-{tone}")


COMPONENTS = (Path(__file__).resolve().parent.parent / "src" / "frontend" / "src" / "components.tsx").read_text()


def test_names_wrap_between_words():
    """A space inside a no-wrap word is no break point, so the space between words must sit outside each word."""
    assert "white-space: nowrap" in rule(".word")
    name = re.search(r"export function Name\b.*?\n}\n", COMPONENTS, re.S).group(0)
    word = re.search(r'<span[^>]*className="word"[^>]*>(.*?)</span>', name, re.S).group(1)
    assert '" "' not in word


def test_ids_stay_on_one_line():
    """A cluster id such as 1026-204044-7mghi0m4 never breaks at its hyphens."""
    assert "white-space: nowrap" in rule("td.mono")
