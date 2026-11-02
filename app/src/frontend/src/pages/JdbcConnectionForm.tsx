import { useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { ErrorBox, Notice, Spinner } from "../components";
import { LoginFields } from "./JdbcLoginFields";

/** The props Discover's create choices receive (the same shape as CreateChoiceProps). A JDBC connection needs none of them:
 *  it saves through the app and reads its own list, so the props are accepted and left unused. */
type ChoiceProps = { workspaceUrl?: string | null; refreshing?: boolean; onRefresh?: () => void };

const SOURCE_TYPES: [string, string][] = [
  ["sqlserver", "SQL Server"], ["oracle", "Oracle"], ["postgresql", "PostgreSQL"], ["mysql", "MySQL"],
];
const KEY_PLACEHOLDER = "orders-login, or creds-{database} for one login per database";

type Connection = { name: string; src_type: string; src_host: string; src_port: number; secret_scope: string;
                    secret_key: string; trust: "verify" | "trust"; created_by: string | null };

type Form = { name: string; src_type: string; src_host: string; src_port: string; secret_scope: string;
              secret_key: string; trust: boolean };

const BLANK: Form = { name: "", src_type: "sqlserver", src_host: "", src_port: "", secret_scope: "", secret_key: "",
                      trust: false };

/** A JDBC connection entered by hand: the admin creates the secret scope and its JSON secret beforehand, and this form
 *  names the scope, the key and the trust choice. It never sees or sends a secret value. */
export function JdbcConnectionForm(_props: ChoiceProps) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<Form>(BLANK);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [made, setMade] = useState<string | null>(null);
  const connections = useQuery({ queryKey: ["jdbc-connections"], queryFn: () => api.get<Row>("/jdbc-connections") });
  const loginScope = useQuery({ queryKey: ["jdbc-login-scope"], queryFn: () => api.get<Row>("/jdbc-logins") });
  const dedicatedScope: string | null = loginScope.data?.scope ?? null;
  const loginsGoHere = dedicatedScope !== null && form.secret_scope.trim() === dedicatedScope;

  const set = (field: keyof Form) => (event: { target: { value: string } }) => {
    setForm({ ...form, [field]: event.target.value });
    setError(null);
    setMade(null);
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const body = { ...form, src_port: form.src_port.trim() === "" ? null : Number(form.src_port) };
      const saved = await api.post<Row>("/jdbc-connections", body);
      setMade(saved.connection.name);
      setForm(BLANK);
      await queryClient.invalidateQueries({ queryKey: ["jdbc-connections"] });
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }

  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState<unknown>(null);
  const [readStarted, setReadStarted] = useState<string | null>(null);

  async function readDatabases(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const chosen = String(data.get("jdbc-read-connection") ?? "");
    const databases = String(data.get("jdbc-read-databases") ?? "").trim();
    setReading(true);
    setReadError(null);
    setReadStarted(null);
    try {
      const started = await api.post<Row>("/discoveries", { jdbc_connection: chosen, databases });
      setReadStarted(`Reading ${started.databases} database${started.databases === 1 ? "" : "s"} through ${chosen} (discovery ${started.discovery_id}).`);
    } catch (e) {
      setReadError(e);
    } finally {
      setReading(false);
    }
  }

  const list: Connection[] = connections.data?.connections ?? [];
  return (
    <div className="jdbc-connection">
      <p>A JDBC connection is a host, a port and a login the admin keeps in a Databricks secret scope. Create the scope and
        its JSON secret (user and password) beforehand; here, name the scope and the key that holds it. Any signed-in
        user can make one, and the app keeps the names, never the secret.</p>
      <form className="jdbc-form" onSubmit={save} aria-label="JDBC connection">
        <label>Name <input value={form.name} onChange={set("name")} placeholder="orders jdbc" required /></label>
        <label>Source type
          <select value={form.src_type} onChange={set("src_type")}>
            {SOURCE_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label>Host <input value={form.src_host} onChange={set("src_host")} placeholder="orders.internal.example" required /></label>
        <label>Port <input inputMode="numeric" value={form.src_port} onChange={set("src_port")} placeholder="blank for the default" /></label>
        <label>Secret scope <input value={form.secret_scope} onChange={set("secret_scope")} placeholder="lakefed" required /></label>
        <label>Secret key <input value={form.secret_key} onChange={set("secret_key")} placeholder={KEY_PLACEHOLDER} required /></label>
        {loginScope.isError && <ErrorBox error={loginScope.error} />}
        {loginsGoHere && <LoginFields connection={form.name} secretKey={form.secret_key} />}
        {dedicatedScope !== null && !loginsGoHere && (
          <p className="muted">A login can be typed here when the secret scope is {dedicatedScope}, the scope the app keeps
            logins in. A login in any other scope is put there by an admin.</p>
        )}
        <fieldset className="radio-body">
          <legend>Server certificate</legend>
          <label className="radio-row"><input type="radio" name="jdbc-trust" checked={!form.trust}
            onChange={() => setForm({ ...form, trust: false })} /> Verify it (recommended)</label>
          <label className="radio-row"><input type="radio" name="jdbc-trust" checked={form.trust}
            onChange={() => setForm({ ...form, trust: true })} /> Trust it, without checking</label>
        </fieldset>
        <div className="toolbar">
          <button className="primary" type="submit" disabled={saving}>{saving ? "Saving" : "Make the connection"}</button>
        </div>
      </form>
      <ErrorBox error={error} />
      {made && <Notice tone="ok">Made {made}. It is in the list below, with its scope and key names.</Notice>}
      <h4>JDBC connections</h4>
      {connections.isError && <ErrorBox error={connections.error} />}
      {list.length === 0 && !connections.isLoading && <p className="muted">None yet.</p>}
      {list.length > 0 && (
        <table className="jdbc-list">
          <thead>
            <tr><th>Name</th><th>Source</th><th>Host</th><th>Secret scope</th><th>Secret key</th><th>Certificate</th></tr>
          </thead>
          <tbody>
            {list.map((c) => (
              <tr key={c.name}>
                <td>{c.name}</td>
                <td>{SOURCE_TYPES.find(([value]) => value === c.src_type)?.[1] ?? c.src_type}</td>
                <td>{c.src_host}:{c.src_port}</td>
                <td>{c.secret_scope}</td>
                <td>{c.secret_key}</td>
                <td>{c.trust === "trust" ? "Trusted" : "Verified"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {list.length > 0 && (
        <form className="jdbc-form" onSubmit={readDatabases} aria-label="Read a JDBC connection's databases">
          <h4>Read its databases</h4>
          <label>Connection
            <select name="jdbc-read-connection" defaultValue={list[0].name}>
              {list.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </label>
          <label>Databases <input name="jdbc-read-databases" placeholder="sales, hr" required /></label>
          <p className="muted">Names, comma or newline separated. The read runs as the discover job, with the connection's scope and certificate choice.</p>
          <div className="toolbar">
            <button className="primary" type="submit" disabled={reading}>{reading ? "Starting" : "Read the databases"}</button>
          </div>
          {reading && <Spinner />}
          <ErrorBox error={readError} />
          {readStarted && <Notice tone="ok">{readStarted}</Notice>}
        </form>
      )}
    </div>
  );
}

/** A job's JDBC connection, on its page and in Edit: where the source is and which scope and key hold its login. Names
 *  only; the secret itself is in the scope and is never shown here. */
export function JdbcSourceNotice({ jdbc }: { jdbc: Row }) {
  if (jdbc.missing) return <Notice tone="warn">It reads through the JDBC connection {jdbc.name}, which is no longer kept.</Notice>;
  const type = SOURCE_TYPES.find(([value]) => value === jdbc.src_type)?.[1] ?? jdbc.src_type;
  return (
    <Notice tone="info">
      It reads through the JDBC connection <b>{jdbc.name}</b>: {type} at {jdbc.src_host}:{jdbc.src_port}, secret scope{" "}
      <code>{jdbc.secret_scope}</code>, key <code>{jdbc.secret_key}</code>, server certificate{" "}
      {jdbc.trust === "trust" ? "trusted without checking" : "verified"}.
    </Notice>
  );
}
