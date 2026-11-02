// A new ingestion job's name, as the Databricks Jobs editor starts a new job: a default name with the date and time
// ("New Job <date> <time>", https://learn.microsoft.com/en-us/azure/databricks/jobs/configure-job), which the user may
// type over in the page's title. Pure, so the Node tests read it as it is (tests/js/job_name.test.mjs).

const two = (n: number) => String(n).padStart(2, "0");

/** "Ingestion job 2026-10-30 11:16:42": the date and time in digits only, so the job's groups, named after it in lower
 * case with each run of other characters as one underscore, come out clean: ingestion_job_2026_10_30_11_16_42_... */
export function defaultJobName(at: Date = new Date()): string {
  return `Ingestion job ${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())} `
    + `${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`;
}

/** Whether the preview refused the name: a group named after it is another job's. */
export function nameTaken(invalid: { errors?: string[] }[] | undefined): boolean {
  return (invalid ?? []).some((line) => (line.errors ?? []).some((text) => text.includes(TAKEN)));
}

/** How the server says a group named after the job is another job's (new_job.plan, by_database). */
const TAKEN = "is already in use: give the job another name";
