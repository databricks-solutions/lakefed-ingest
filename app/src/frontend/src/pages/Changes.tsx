import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorBox, Grid, Section, autoColumns } from "../components";

export default function Changes() {
  const changes = useQuery({ queryKey: ["changes"], queryFn: () => api.get("/changes?limit=500"), refetchInterval: 30_000 });
  return (
    <>
      <h1>Changes</h1>
      <ErrorBox error={changes.error} />
      <Section title="Recent changes to control rows">
        <p className="muted small">Every add, register, edit, enable / disable and reseed made in this app: who, what and when.
          Edits made directly with SQL aren't listed, but the job still reseeds a table whose source, sink or key changed.</p>
        <Grid rows={changes.data} height={620}
          columns={autoColumns(changes.data ?? [], { old_value: { maxWidth: 360, tooltipField: "old_value" },
            new_value: { maxWidth: 360, tooltipField: "new_value" } })} />
      </Section>
    </>
  );
}
