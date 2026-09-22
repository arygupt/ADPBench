/* Replay evidence annotates a frozen result; it never changes its score. */
export function findReplay(evidence, pilot, run) {
  if (evidence?.schema_version !== 1 || evidence.pilot !== pilot ||
      !Array.isArray(evidence.runs) || !/^[a-f0-9]{64}$/.test(run.submission_sha256))
    return null;
  const entry = evidence.runs.find((e) =>
    e && e.label === run.label && e.problem === run.problem &&
    e.attempt === run.attempt && e.submission_sha256 === run.submission_sha256 &&
    ["correct", "cells", "cycles", "ratio"].every((key) => e.recorded?.[key] === run[key]),
  );
  const replay = entry?.replay;
  if (!replay || replay.match !== true ||
      !/^[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+$/.test(replay.repository) ||
      ![replay.run_id, replay.run_attempt, replay.job_id].every((n) => Number.isSafeInteger(n) && n > 0) ||
      !/^[a-f0-9]{40}$/.test(replay.commit) ||
      !/^pilot\/results\/[A-Za-z0-9_-]+\/submissions\/[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9_-]+\.v$/.test(replay.submission_path) ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(replay.completed_at))
    return null;
  const repo = `https://github.com/${replay.repository}`;
  const workflow = `${repo}/actions/runs/${replay.run_id}/attempts/${replay.run_attempt}`;
  return {
    date: replay.completed_at.slice(0, 10),
    job: `${repo}/actions/runs/${replay.run_id}/job/${replay.job_id}`,
    workflow,
    source: `${repo}/blob/${replay.commit}/${replay.submission_path}`,
  };
}
// Original model-generation/scoring evidence, never labeled as a replay.
export function findExecution(run) {
  const e = run?.execution;
  if (!e || e.kind !== "model-evaluation" || e.repository !== "arygupt/ADPBench" ||
      ![e.run_id, e.run_attempt, e.job_id].every((n) => Number.isSafeInteger(n) && n > 0) ||
      !/^[a-f0-9]{40}$/.test(e.commit) ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(e.completed_at) ||
      !["success", "failure", "timed_out"].includes(e.job_conclusion)) return null;
  const base = `https://github.com/${e.repository}`;
  return {
    job: `${base}/actions/runs/${e.run_id}/job/${e.job_id}`,
    workflow: `${base}/actions/runs/${e.run_id}/attempts/${e.run_attempt}`,
    code: `${base}/tree/${e.commit}`,
    conclusion: e.job_conclusion,
  };
}
