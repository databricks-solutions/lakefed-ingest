import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { JobPageRefresh, type LiveRead } from "./jobPageRefresh";

/** Keep a job's page refreshing together while a run of it is going and once when it ends (see jobPageRefresh.ts).
 * Returns when the page last finished such a read, in milliseconds, or 0 before the first. */
export function useJobPageRefresh(job: string, live: LiveRead | undefined): number {
  const client = useQueryClient();
  const [readAt, setReadAt] = useState(0);
  const refresh = useMemo(() => new JobPageRefresh(client, job, setReadAt), [client, job]);
  useEffect(() => () => refresh.stop(), [refresh]);
  useEffect(() => refresh.update(live), [refresh, live]);
  return readAt;
}
