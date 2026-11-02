// What the signed-in user may do, from their Databricks job permissions (GET /api/permissions). Pages use it to show
// a button disabled with the reason; the server checks every call again, so this is only for the page.
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

type Refusal = string | null;

export interface Permissions {
  jobs: Record<string, { run: Refusal; change: Refusal; delete: Refusal }>;
  groups: Record<string, string>;
  run: { ingestion: Refusal; discovery: Refusal };
  new_running_job: Record<string, Refusal>;
  /** Why the user may not manage the app (Can Manage on the app), or null when they may: Manage shows only then. */
  manage: Refusal;
}

export function usePermissions() {
  return useQuery({ queryKey: ["permissions"], queryFn: () => api.get<Permissions>("/permissions"), staleTime: 15_000 });
}

/** Why the user may not change tables of these groups (the first refusal), or null when they may. */
export function groupsRefusal(permissions: Permissions | undefined, groups: Iterable<unknown>): Refusal {
  if (!permissions) return null;
  for (const group of groups) {
    const refusal = permissions.groups[String(group ?? "")];
    if (refusal) return refusal;
  }
  return null;
}

/** One action's refusal under its buttons, as the job page shows Run now's. */
export function Refused({ action, refusal }: { action: string; refusal: Refusal | undefined }) {
  return refusal ? <p className="muted small right">{action}: {refusal}.</p> : null;
}
