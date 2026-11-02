"""Discover's Create a connection option: a list of ways to make a connection, each with its own short line, a primary
button to Catalog Explorer's connections, a secondary Refresh, and the requirements behind an (i). Plain source checks,
no app run."""
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages"
DISCOVER = PAGES / "Discover.tsx"
CHOICES = PAGES / "CreateConnectionChoices.tsx"
JDBC = PAGES / "JdbcConnectionForm.tsx"


def test_the_create_option_states_its_requirements_behind_an_info_tip():
    text = DISCOVER.read_text()
    assert '<Info label="What a new connection needs">A connection is made in Catalog Explorer' in text
    assert "Making it needs CREATE CONNECTION on the metastore, and this app needs USE CONNECTION on it." in text


def test_the_create_option_is_a_list_of_choices_in_its_own_file():
    discover = DISCOVER.read_text()
    assert 'import { CreateConnectionChoices } from "./CreateConnectionChoices";' in discover
    assert '<CreateConnectionChoices workspaceUrl={config?.workspace_url} refreshing={refreshing} onRefresh={() => { refresh(); setHow("existing"); }} />' in discover
    text = CHOICES.read_text()
    assert "export const CREATE_CHOICES: CreateChoice[] = [" in text
    assert 'role="radiogroup"' in text
    assert 'name="create-choice"' in text


def test_the_menu_has_two_ways_through_unity_catalog_and_a_jdbc_connection_entered_by_hand():
    text = CHOICES.read_text()
    assert 'import { JdbcConnectionForm } from "./JdbcConnectionForm";' in text
    assert '{ id: "catalog-explorer", label: "Through Unity Catalog", Body: CatalogExplorerChoice }' in text
    assert '{ id: "jdbc", label: "JDBC connection entered by hand", Body: JdbcConnectionForm }' in text


def test_discover_has_no_second_radio_for_the_jdbc_form():
    discover = DISCOVER.read_text()
    assert "create-via" not in discover
    assert "createVia" not in discover
    assert "JdbcConnectionForm" not in discover


def test_the_jdbc_form_takes_the_choice_props_and_needs_none_of_them():
    text = JDBC.read_text()
    assert "type ChoiceProps = { workspaceUrl?: string | null; refreshing?: boolean; onRefresh?: () => void };" in text
    assert "export function JdbcConnectionForm(_props: ChoiceProps)" in text


def test_the_open_button_is_primary_and_the_refresh_is_secondary():
    text = CHOICES.read_text()
    assert '<button className="primary" onClick={() => window.open(`${workspaceUrl}/explore/connections`, "_blank", "noopener")}>' in text
    assert "Open connections in Catalog Explorer</button>" in text
    assert '<button className="ghost" disabled={refreshing} onClick={onRefresh}>Refresh</button>' in text


def test_the_old_long_paragraph_and_wording_are_gone():
    text = DISCOVER.read_text() + CHOICES.read_text()
    assert "I made it: refresh the list" not in text
    assert "Make it there, give this app USE CONNECTION on it" not in text
    assert "Open Connections in Catalog Explorer" not in text
