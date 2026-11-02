import { useState, type KeyboardEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { ErrorBox, Notice, Spinner, fmtTime } from "../components";

/** The login a secret key holds: which scope, who wrote it and when. The password is never read back. */
type SavedLogin = { scope: string; secret_key: string; entered_by: string; entered_at: string };

const DATABASE_TOKEN = "{database}";

/** The user name and password for a JDBC connection's login. They are written into the dedicated secret scope as the
 *  app's own identity, so the password never leaves the app for the scope. The field is cleared after a save and is
 *  never prefilled. Its own button saves it, and Enter inside it is held back so it doesn't submit the connection form
 *  around it. A key with {database} in it takes the database the login is for: the login is written under the key with
 *  that name filled in, the key a discovery reads for that database. */
export function LoginFields({ connection, secretKey }: { connection: string; secretKey: string }) {
  const queryClient = useQueryClient();
  const template = secretKey.trim();
  const needsDatabase = template.includes(DATABASE_TOKEN);
  const [database, setDatabase] = useState("");
  const databaseName = database.trim();
  const key = needsDatabase ? (databaseName === "" ? "" : template.split(DATABASE_TOKEN).join(databaseName)) : template;
  const [userName, setUserName] = useState("");
  const [password, setPassword] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [justSaved, setJustSaved] = useState<SavedLogin | null>(null);
  const latest = useQuery({
    queryKey: ["jdbc-login", key],
    queryFn: () => api.get<Row>(`/jdbc-logins${qs({ secret_key: key })}`),
    enabled: key !== "",
  });

  const fromServer = latest.data?.login
    ? { scope: latest.data.scope, secret_key: latest.data.secret_key, ...latest.data.login }
    : null;
  const current: SavedLogin | null = justSaved?.secret_key === key ? justSaved : fromServer;
  const canSave = connection.trim() !== "" && key !== "" && userName.trim() !== "" && password !== "" && !saving;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const saved = await api.post<SavedLogin>("/jdbc-logins", {
        connection: connection.trim(),
        secret_key: template,
        database: needsDatabase ? databaseName : null,
        user_name: userName.trim(),
        password,
      });
      setJustSaved(saved);
      setPassword("");
      setReplacing(false);
      await queryClient.invalidateQueries({ queryKey: ["jdbc-login", key] });
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  }

  function holdEnter(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (canSave) void save();
  }

  const writing = current === null || replacing;
  return (
    <fieldset className="radio-body jdbc-login">
      <legend>Login</legend>
      {template === "" && <p className="muted">Name the secret key above to write its login.</p>}
      {needsDatabase && (
        <label>Database the login is for
          <input name="jdbc-login-database" spellCheck={false} value={database}
            onChange={(event) => setDatabase(event.target.value)} onKeyDown={holdEnter} />
        </label>
      )}
      {template !== "" && key === "" && <p className="muted">Name the database this login is for, to write its login.</p>}
      {latest.isLoading && <Spinner />}
      {latest.isError && <ErrorBox error={latest.error} />}
      {key !== "" && current && !replacing && (
        <>
          <Notice tone="ok">
            Login saved in {current.scope}/{current.secret_key} by {current.entered_by} at {fmtTime(current.entered_at)}.
          </Notice>
          <div className="toolbar">
            <button type="button" onClick={() => setReplacing(true)}>Replace the login</button>
          </div>
        </>
      )}
      {key !== "" && writing && (
        <>
          <label>User name
            <input name="jdbc-login-user" autoComplete="off" spellCheck={false} value={userName}
              onChange={(event) => setUserName(event.target.value)} onKeyDown={holdEnter} />
          </label>
          <label>Password
            <input type="password" name="jdbc-login-password" autoComplete="off" value={password}
              onChange={(event) => setPassword(event.target.value)} onKeyDown={holdEnter} />
          </label>
          <p className="muted">The password goes to the secret scope as the app and is not kept here or shown again.</p>
          <div className="toolbar">
            {replacing && (
              <button type="button" onClick={() => { setReplacing(false); setPassword(""); setError(null); }}>Cancel</button>
            )}
            <button type="button" className="primary" disabled={!canSave} onClick={() => void save()}>
              {saving ? "Saving" : "Save the login"}
            </button>
          </div>
        </>
      )}
      <ErrorBox error={error} />
    </fieldset>
  );
}
