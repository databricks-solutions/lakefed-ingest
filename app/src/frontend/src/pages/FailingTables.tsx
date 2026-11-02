import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { ErrorBox, Notice, Section, fmtInt, fmtTime, plural, useConfirm, type ConfirmRequest } from "../components";

// What each kind of failure means for the table, as its title reads on the page.
const KIND_NOTE: Record<string, string> = {
  transient: "A failure that may clear on its own, such as a dropped connection or a timeout. Retried only while the "
    + "job's table retry budget is above 0; at the default, 0, it fails at once.",
  permanent: "A failure that needs a change at the source or in the job, such as bad data or a schema change.",
  unknown: "A failure no rule recognised: retried once in its run while the job's table retry budget is above 0, "
    + "then failed. At 0, the default, it fails at once.",
};

const SHOWN_MESSAGE = 240;

/** The job's failing tables: those whose last load failed and that have not loaded since. Shown only when there are some.
 * Retry now starts one ingest run for the table's database alone; Disable turns this table off in this job, in this
 * database; Remove deletes the table's ingestion state from the job (the delete job does it) and keeps its target table.
 * Each row says what failed, its class and kind, when it began failing and how many runs failed it. */
export function FailingTables({ jobId, runRefused, changeRefused, removeRefused }: { jobId: string;
  runRefused?: string | null; changeRefused?: string | null; removeRefused?: string | null }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const failing = useQuery({
    queryKey: ["ingestion-job-failing", jobId],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(jobId)}/failing-tables`),
    refetchInterval: 15_000,
  });
  const refreshJob = () => {
    qc.invalidateQueries({ queryKey: ["ingestion-job-failing", jobId] });
    ["ingestion-job", "ingestion-job-live", "ingestion-job-runs"].forEach((key) => qc.invalidateQueries({ queryKey: [key, jobId] }));
  };
  const retry = useMutation({
    mutationFn: (row: Row) => api.post<Row>(`/ingestion-jobs/${encodeURIComponent(jobId)}/failing-tables/retry`,
      { task_collection: row.task_collection, server: row.server, database: row.src_database }),
    onSuccess: refreshJob,
  });
  const disable = useMutation({
    mutationFn: (row: Row) => api.post<Row>(`/ingestion-jobs/${encodeURIComponent(jobId)}/enable`,
      { server: row.server, database: row.src_database, schema: row.src_schema, table: row.src_table, enabled: false }),
    onSuccess: refreshJob,
  });
  const remove = useMutation({
    mutationFn: (row: Row) => api.post<Row>(`/ingestion-jobs/${encodeURIComponent(jobId)}/tables/remove`,
      { control_ids: [row.control_id], confirmed: true }),
    onSuccess: refreshJob,
  });
  const tables: Row[] = failing.data?.tables ?? [];
  if (!tables.length) return failing.error ? <ErrorBox error={failing.error} /> : null;
  return (
    <Section title={`Failing tables: ${fmtInt(tables.length)}`}
      note="Tables whose last load failed and that have not loaded since. Retry now starts one run for the table's database alone; Disable turns this table off in this database; Remove deletes its ingestion state from the job and keeps its target table.">
      {retry.isSuccess && <Notice tone="ok">Started a run of {retry.variables?.src_database}: run {retry.data.started.run_id}.</Notice>}
      {disable.isSuccess && <Notice tone="ok">Disabled {disable.variables?.src_table} in {disable.variables?.src_database}. To turn it back on, use Enabled on the job's Edit page.</Notice>}
      {remove.data?.error && <Notice tone="bad">{remove.data.error}</Notice>}
      <ErrorBox error={retry.error} />
      <ErrorBox error={disable.error} />
      <ErrorBox error={remove.error} />
      <div className="table-wrap"><table className="data dense wrap-head">
        <thead><tr><th>Table and database</th><th>Error class</th><th>Kind</th><th>Source message</th><th>Failing since</th>
          <th className="num">Runs</th><th></th></tr></thead>
        <tbody>
          {tables.map((row) => {
            const retrying = retry.isPending && retry.variables?.control_id === row.control_id;
            const disabling = disable.isPending && disable.variables?.control_id === row.control_id;
            const removingNow = row.remove?.state === "removing"
              || (remove.isPending && remove.variables?.control_id === row.control_id);
            return (
              <tr key={row.control_id}>
                <td className="name"><b>{row.table}</b>
                  <span className="sub">{row.src_database} on {row.server}</span>
                  <span className="sub">ingestion group {row.task_collection}</span></td>
                <td>{row.error_class}</td>
                <td title={row.error_kind ? KIND_NOTE[row.error_kind] : "Recorded for failures after this version; not known for earlier ones."}>
                  {row.error_kind ?? <span className="muted">not recorded</span>}</td>
                <td className="small" title={row.message}>{row.message.length > SHOWN_MESSAGE ? `${row.message.slice(0, SHOWN_MESSAGE)}…` : row.message}</td>
                <td>{row.failing_since ? fmtTime(row.failing_since)
                  : <span className="muted">before the last 30 days</span>}</td>
                <td className="num">{fmtInt(row.runs)} {plural(row.runs, "run")}
                  <span className="sub">{fmtInt(row.failures)} {plural(row.failures, "failure")}</span></td>
                <td className="actions">
                  <button className="ghost" disabled={!!runRefused || retrying}
                    title={runRefused ?? "Start one ingest run for this table's database alone"}
                    onClick={() => confirm.ask(retryRequest(row)).then((ok) => ok && retry.mutate(row))}>
                    {retrying ? "Starting…" : "Retry now"}</button>{" "}
                  <button className="ghost" disabled={!!changeRefused || disabling}
                    title={changeRefused ?? "Turn this table off in this job, in this database"}
                    onClick={() => disable.mutate(row)}>{disabling ? "Disabling…" : "Disable"}</button>{" "}
                  <button className="danger-text" disabled={!!removeRefused || removingNow}
                    title={removeRefused ?? "Delete this table's ingestion state from the job; its target table is kept"}
                    onClick={() => confirm.ask(removeRequest(row)).then((ok) => ok && remove.mutate(row))}>Remove</button>
                  {removingNow && <span className="sub">Removing</span>}
                  {row.remove?.state === "failed" && <span className="sub danger-text">{row.remove.message}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table></div>
      {confirm.dialog}
    </Section>
  );
}

// What Remove asks first: the table's ingestion state leaves the job, and its target table is kept.
function removeRequest(row: Row): ConfirmRequest {
  return { title: `Remove ${row.table} from this job?`, action: "Remove", danger: true,
    body: <><p>This deletes {row.table}'s ingestion state from this job: its control row, its checkpoint and its queued
      reseeds. This can't be undone. The job's other tables and its run history stay.</p>
      <p>The target table is kept: nothing is dropped or changed in it.</p></> };
}

// What Retry now starts, for its dialog: one ingest run of one database, on its own job cluster, which costs money.
function retryRequest(row: Row): ConfirmRequest {
  return { title: `Retry ${row.src_database} now?`, action: "Retry now",
    body: <><p>This starts one ingest run for {row.src_database} on {row.server}, and no other database. Its enabled tables load
      from their checkpoints on one job cluster.</p>
      <p>The cluster and the source's load cost money while it runs.</p></> };
}
