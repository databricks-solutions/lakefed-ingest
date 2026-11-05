import type { ReactNode } from "react";
import type { Row } from "../api";
import { CopyButton, Cut } from "../components";
import { connectionTypeName, hostLabel } from "../cells";

/** The Unity Catalog connections to choose from, one line to a row as Catalog Explorer's connections list is: the name
 * and host take the room there is, the type has a narrow column of its own and is never broken, and the owner is cut
 * with an ellipsis. Anything cut shows whole on hover or focus, a click on any of a row's text chooses it, and the
 * host, with its port, can be copied without choosing it. `empty` is the note shown in place of rows when there are none. */
export function ConnectionTable({ rows, chosen, disabled, onChoose, empty }: {
  rows: Row[]; chosen: string; disabled: boolean; onChoose: (name: string) => void; empty: ReactNode;
}) {
  return (
    <div className="table-wrap"><table className="data one-line connections">
      <colgroup><col className="col-pick" /><col className="col-name" /><col className="col-type" /><col className="col-host" /><col className="col-owner" /></colgroup>
      <thead><tr><th></th><th>Name</th><th>Type</th><th>Host</th><th>Owner</th></tr></thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.name} className="pick-row" onClick={() => { if (!disabled) onChoose(c.name); }}>
            <td><input type="radio" checked={chosen === c.name} readOnly aria-label={`Use ${c.name}`} /></td>
            <td><b><Cut text={c.name} tipOnlyWhenCut passClick /></b></td>
            <td><Cut text={connectionTypeName(c.connection_type)} tipOnlyWhenCut passClick /></td>
            <td className="mono">{c.host && <span className="cell-line">
              <Cut text={hostLabel(c.host, c.port)} tipOnlyWhenCut passClick />
              <CopyButton text={hostLabel(c.host, c.port)} label={`Copy host of ${c.name}`} /></span>}</td>
            <td><Cut text={c.owner ?? ""} tipOnlyWhenCut passClick /></td>
          </tr>
        ))}
        {empty && <tr><td colSpan={5} className="muted">{empty}</td></tr>}
      </tbody>
    </table></div>
  );
}
