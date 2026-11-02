import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { ErrorBox, OutLink, Pager, Section, StateBar, StateKey, fmtAgo, fmtInt, plural, Twisty } from "../components";

// A job's target tables: where its data lands, each with its state and the rows the run records say were written over
// 7 days. Opening one lists the source tables and databases written into it. Names follow Catalog Explorer.
// Exact row counts would need a SQL warehouse, which the app avoids; Catalog Explorer has them, one click away.

const PAGE = 25;

function SourceRows({ jobId, target, t }: { jobId: string; target: string; t: Row }) {
  const [offset, setOffset] = useState(0);
  const sources = useQuery({
    queryKey: ["job-target-sources", jobId, target, offset],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${jobId}/targets/sources${qs({ target, offset, limit: PAGE })}`),
  });
  const outside = Number(t.outside_sources);
  return (
    <>
      <tr className="l2"><td colSpan={4} className="muted">{fmtInt(t.sources)} {plural(t.sources, "source table")} from{" "}
        {fmtInt(t.databases)} {plural(t.databases, "database")}{Number(t.with_client_id) > 0 ? ", kept apart by client_id" : ""}
        {outside > 0 && <> <span className="tag warn">{fmtInt(outside)} more {plural(outside, "source table")} of other
          jobs {outside === 1 ? "writes" : "write"} here too</span></>}</td></tr>
      {(sources.data?.items ?? []).map((s: Row) => (
        <tr key={s.id} className="l2">
          <td className="name">{s.server} › {s.database} › {s.schema}.{s.table}
            {s.client_id && <span className="sub">client_id {s.client_id}</span>}</td>
          <td>{s.enabled ? <StateBar row={s} /> : <span className="muted">disabled</span>}</td>
          <td className="num">{s.rows_written_7d != null ? fmtInt(s.rows_written_7d) : ""}
            {s.last_written && <span className="sub">last {fmtAgo(s.last_written)}</span>}</td>
          <td></td>
        </tr>
      ))}
      {(sources.data?.total ?? 0) > PAGE && (
        <tr className="l2"><td colSpan={4}><Pager offset={offset} limit={PAGE} total={sources.data!.total} noun="source table" onPage={setOffset} /></td></tr>
      )}
      {sources.error && <tr className="l2"><td colSpan={4}><ErrorBox error={sources.error} /></td></tr>}
    </>
  );
}

export function TargetTables({ jobId }: { jobId: string }) {
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const targets = useQuery({
    queryKey: ["job-targets", jobId, search, offset],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${jobId}/targets${qs({ search, offset, limit: PAGE })}`),
  });
  const total = targets.data?.total;
  return (
    <Section title={`Target tables${total != null ? `: ${fmtInt(total)}` : ""}`} actions={<StateKey />}
      note="Where this job's data lands. Open a target for the source tables and databases written into it. Rows written are from the run records over the last 7 days; Catalog Explorer has each table's exact size.">
      <input className="search tree-search" placeholder="Find a target table" value={search}
        onChange={(e) => { setSearch(e.target.value); setOffset(0); setOpen(null); }} />
      <div className="table-wrap"><table className="data tree">
        <thead><tr><th>Name › its source tables</th><th>State now</th><th className="num">Rows written (7 days)</th><th></th></tr></thead>
        <tbody>
          {(targets.data?.items ?? []).map((t: Row) => (
            <Fragment key={t.target}>
              <tr>
                <td className="name"><Twisty open={open === t.target} onToggle={() => setOpen(open === t.target ? null : t.target)}><b>{t.table}</b></Twisty>
                  <span className="sub target-path">{t.catalog}.{t.schema}</span></td>
                <td><StateBar row={t} /></td>
                <td className="num">{t.rows_written_7d != null ? fmtInt(t.rows_written_7d) : ""}
                  <span className="sub">{t.last_written ? `last ${fmtAgo(t.last_written)}` : "none in 7 days"}</span></td>
                <td><OutLink href={t.url}>Catalog Explorer</OutLink></td>
              </tr>
              {open === t.target && <SourceRows jobId={jobId} target={t.target} t={t} />}
            </Fragment>
          ))}
          {targets.data && !targets.data.items.length && (
            <tr><td colSpan={4} className="muted">{search ? "No target table matches." : "No tables yet."}</td></tr>
          )}
        </tbody>
      </table></div>
      {(total ?? 0) > PAGE && <Pager offset={offset} limit={PAGE} total={total!} noun="target table" onPage={setOffset} />}
      <ErrorBox error={targets.error} />
    </Section>
  );
}
