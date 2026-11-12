import { useQuery } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { Modal } from "../components";
import { runNowFallbackLine, runNowLine, type RunPreview } from "../runDialog";

/** Run now's confirmation: one line saying which tables the run loads and where from. The tables are read when the
 * dialog opens (the run preview, one read of the job's control rows); until they are, Run now stays off and the dialog
 * says so, as Delete's does. If they can't be read the line comes from what the page already has, and Run now stays
 * available: a failed read never stops a run. */
export function RunNowDialog({ job, onClose }: { job: Row; onClose: (confirmed: boolean) => void }) {
  const preview = useQuery({ queryKey: ["run-preview", job.id], retry: false, gcTime: 0,
    queryFn: () => api.get<RunPreview>(`/ingestion-jobs/${encodeURIComponent(job.id)}/run-preview`) });
  const check = Boolean(job.run_now?.check);
  const reading = preview.isLoading;
  const line = preview.data ? runNowLine(preview.data, check)
    : runNowFallbackLine(Number(job.enabled_tables ?? 0), (job.groups ?? []) as string[], check);
  return (
    <Modal title={`Run ${job.name} now?`} action="Run now" actionDisabled={reading ? "Reading the tables it loads…" : undefined}
      onClose={onClose}>
      {reading ? <p className="muted">Reading the tables it loads…</p> : <p>{line}</p>}
    </Modal>
  );
}
