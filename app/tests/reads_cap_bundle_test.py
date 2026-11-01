"""The app bundle's reads_start_cap and reads_max_cap: variables (8 and 32 by default) passed to the app as
READS_START_CAP and READS_MAX_CAP, which warehouse_reads reads within bounds. Read as text, like the other bundle
tests."""
from pathlib import Path

import warehouse_reads as wr

APP_BUNDLE = Path(__file__).resolve().parents[1]
VARIABLES = (APP_BUNDLE / "databricks.yml").read_text()
RESOURCES = (APP_BUNDLE / "resources" / "lakefed_ingest_app.yml").read_text()


def _block(name):
    """A variable's lines, up to its default."""
    start = VARIABLES.index(f"\n  {name}:\n")
    return VARIABLES[start:VARIABLES.index("\n", VARIABLES.index("    default:", start)) + 1]


def test_the_variables_exist_with_the_starting_cap_and_its_ceiling():
    assert _block("reads_start_cap").endswith("    default: 8\n")
    assert _block("reads_max_cap").endswith("    default: 32\n")
    assert "never below 4" in " ".join(_block("reads_start_cap").split())


def test_the_app_is_told_them():
    assert "          - name: READS_START_CAP\n            value: ${var.reads_start_cap}\n" in RESOURCES
    assert "          - name: READS_MAX_CAP\n            value: ${var.reads_max_cap}\n" in RESOURCES


def test_the_settings_are_read_within_bounds(monkeypatch):
    monkeypatch.setenv("READS_START_CAP", "12")
    assert wr._setting("READS_START_CAP", 8, wr.MIN_CAP, 32) == 12
    monkeypatch.setenv("READS_START_CAP", "1")
    assert wr._setting("READS_START_CAP", 8, wr.MIN_CAP, 32) == wr.MIN_CAP
    monkeypatch.setenv("READS_START_CAP", "99")
    assert wr._setting("READS_START_CAP", 8, wr.MIN_CAP, 32) == 32
    monkeypatch.setenv("READS_START_CAP", "eight")
    assert wr._setting("READS_START_CAP", 8, wr.MIN_CAP, 32) == 8
    monkeypatch.setenv("READS_START_CAP", "")
    assert wr._setting("READS_START_CAP", 8, wr.MIN_CAP, 32) == 8
