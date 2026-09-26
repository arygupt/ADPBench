// Links to a run's original model-generation/scoring evidence on GitHub.
// This is the original run, never labeled as a replay.

const REPOSITORY = "arygupt/ADPBench";
const CONCLUSIONS = ["success", "failure", "timed_out", "cancelled"];
const isPositiveId = (n) => Number.isSafeInteger(n) && n > 0;

function isValidExecution(e) {
  return (
    e &&
    e.kind === "model-evaluation" &&
    e.repository === REPOSITORY &&
    [e.run_id, e.run_attempt, e.job_id].every(isPositiveId) &&
    /^[a-f0-9]{40}$/.test(e.commit) &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(e.completed_at) &&
    CONCLUSIONS.includes(e.job_conclusion)
  );
}

/** Returns { job, workflow, code, conclusion } URLs for a run, or null if unverifiable. */
export function findExecution(run) {
  const e = run?.execution;
  if (!isValidExecution(e)) return null;
  const base = `https://github.com/${e.repository}`;
  return {
    job: `${base}/actions/runs/${e.run_id}/job/${e.job_id}`,
    workflow: `${base}/actions/runs/${e.run_id}/attempts/${e.run_attempt}`,
    code: `${base}/tree/${e.commit}`,
    conclusion: e.job_conclusion,
  };
}
