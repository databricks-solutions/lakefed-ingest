"""Manage's SQL warehouse (src/frontend/src/pages/Manage.tsx): one read-only row for the warehouse the app's queries run
on, from GET /api/manage/version, with its name and id, its type, its state, the setting that chose it and the problem
line when there is one. The words come from manageHelpers.ts, which manage_helpers.test.mjs checks."""
import re
from pathlib import Path

MANAGE = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages" / "Manage.tsx"


def _warehouse_section() -> str:
    source = MANAGE.read_text()
    found = re.search(r"\nfunction Warehouse\(\) \{\n(.*?)\n\}\n", source, re.S)
    assert found, "Manage.tsx has no Warehouse section"
    return found.group(1)


def test_manage_shows_the_warehouse_after_the_version():
    source = MANAGE.read_text()
    assert source.index("<Version />") < source.index("<Warehouse />") < source.index("<Export />")


def test_the_warehouse_row_shows_name_and_id_type_state_setting_and_problem():
    section = _warehouse_section()
    assert '<Section title="SQL warehouse">' in section
    assert "<th>SQL warehouse</th><th>Warehouse ID</th><th>Type</th><th>State</th><th>Set by</th>" in section
    assert section.count("<tr>") == 2                                           # the header and one row
    for shown in ("warehouse.name", "warehouse.id", "warehouseKind(warehouse)", "warehouse.state",
                  "warehouseChosenBy(warehouse)", '<Notice tone="warn">{warehouse.problem}</Notice>'):
        assert shown in section, shown


def test_the_warehouse_is_read_only_and_read_with_the_version():
    section = _warehouse_section()
    for control in ("<button", "<input", "<select", "useMutation", "api.post", "onClick"):
        assert control not in section, control
    assert "useQuery(versionQuery)" in section
    assert 'queryKey: ["manage-version"]' in MANAGE.read_text()
