import { useState, type ComponentType } from "react";
import { JdbcConnectionForm } from "./JdbcConnectionForm";

/** What every way to make a connection receives from Discover: the workspace link, whether a refresh is running, and what
 *  to do after a refresh (back to the list of existing connections). */
export type CreateChoiceProps = { workspaceUrl?: string | null; refreshing: boolean; onRefresh: () => void };

/** One way to make a connection, listed under Discover's Create a connection. Body draws the buttons or form for that way. */
export type CreateChoice = { id: string; label: string; Body: ComponentType<CreateChoiceProps> };

function CatalogExplorerChoice({ workspaceUrl, refreshing, onRefresh }: CreateChoiceProps) {
  return (
    <>
      <p className="muted">Make it in Catalog Explorer, then come back here and choose it above.</p>
      <div className="toolbar">
        {workspaceUrl && <button className="primary" onClick={() => window.open(`${workspaceUrl}/explore/connections`, "_blank", "noopener")}>
          Open connections in Catalog Explorer</button>}
        <button className="ghost" disabled={refreshing} onClick={onRefresh}>Refresh</button>
      </div>
    </>
  );
}

/** A new way to make a connection is one more entry here, in its own component file. */
export const CREATE_CHOICES: CreateChoice[] = [
  { id: "catalog-explorer", label: "Through Unity Catalog", Body: CatalogExplorerChoice },
  { id: "jdbc", label: "JDBC connection entered by hand", Body: JdbcConnectionForm },
];

/** Discover's Create a connection body: the ways to make one, as a list with one picked at a time. */
export function CreateConnectionChoices(props: CreateChoiceProps) {
  const [chosenId, setChosenId] = useState(CREATE_CHOICES[0].id);
  const chosen = CREATE_CHOICES.find((choice) => choice.id === chosenId) ?? CREATE_CHOICES[0];
  const Body = chosen.Body;
  return (
    <div className="radio-body" role="radiogroup" aria-label="How to make the connection">
      {CREATE_CHOICES.map((choice) => (
        <label key={choice.id} className="radio-row">
          <input type="radio" name="create-choice" checked={choice.id === chosen.id} onChange={() => setChosenId(choice.id)} /> {choice.label}
        </label>
      ))}
      <Body {...props} />
    </div>
  );
}
