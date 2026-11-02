import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { heading, PageHead, readAt, ErrorBox, Grid, Section, autoColumns, fmtShort, fmtTime, usePageStamp } from "../components";

// Each change's action in words, as the Databricks audit and history views name events.
const ACTIONS: Record<string, string> = {
  add: "Table added", register: "Table registered", import: "Tables imported", edit: "Table edited", enable: "Enabled",
  disable: "Disabled", reseed: "Reseed requested", schedule_create: "Job created", schedule_edit: "Job edited",
  schedule_pause: "Job paused", schedule_resume: "Job resumed", schedule_delete: "Job deleted", schedule_sync: "Databricks jobs synced",
};

export default function Changes() {
  const changes = useQuery({ queryKey: ["changes"], queryFn: () => api.get("/changes?limit=500"), refetchInterval: 30_000 });
  usePageStamp(readAt(changes), 30, changes.error);
  return (
    <>
      <PageHead title="Changes" lead="Every change made in this app, newest first: who, what and when. Edits made directly with SQL aren't listed." />
      <ErrorBox error={changes.error} />
      <Section title="Recent changes">
        <Grid rows={changes.data}
          columns={autoColumns(changes.data ?? [], { old_value: { maxWidth: 360, tooltipField: "old_value" },
            new_value: { maxWidth: 360, tooltipField: "new_value" },
            changed_at: { valueFormatter: (p) => fmtShort(p.value), tooltipValueGetter: (p) => fmtTime(p.value) },
            action: { valueFormatter: (p) => ACTIONS[p.value] ?? p.value },
            field: { valueFormatter: (p) => (p.value ? heading(String(p.value)) : "") } })} />
      </Section>
    </>
  );
}
