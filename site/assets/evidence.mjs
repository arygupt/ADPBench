// Original model-generation/scoring evidence, never labeled as a replay.
export function findExecution(run) {
  const e = run?.execution;
  if (!e || e.kind !== "model-evaluation" || e.repository !== "arygupt/ADPBench" ||
      ![e.run_id, e.run_attempt, e.job_id].every((n) => Number.isSafeInteger(n) && n > 0) ||
      !/^[a-f0-9]{40}$/.test(e.commit) ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(e.completed_at) ||
      !["success", "failure", "timed_out", "cancelled"].includes(e.job_conclusion)) return null;
  const base = `https://github.com/${e.repository}`;
  return {
    job: `${base}/actions/runs/${e.run_id}/job/${e.job_id}`,
    workflow: `${base}/actions/runs/${e.run_id}/attempts/${e.run_attempt}`,
    code: `${base}/tree/${e.commit}`,
    conclusion: e.job_conclusion,
  };
}
