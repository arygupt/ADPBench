// Typed agent outcomes are independent of GitHub job color and unknown scores.
export function agentState(run) {
  if (run?.generation?.protocol !== "agent-assisted-v1") return null;
  const states = {
    correct: ["correct", "Correct", "pass"],
    incorrect: ["fail", "Incorrect RTL", "wrong"],
    invalid_submission: ["fail", "Invalid submission", "invalid"],
    audit_rejected: ["fail", "Submission rejected by audit", "rejected"],
    turn_limit: ["fail", "Agent turn limit reached", "turn limit"],
    truncated: ["fail", "Provider output truncated", "truncated"],
    wall_timeout: ["fail", "Agent time limit reached", "time limit"],
    provider_error: ["infra", "Provider request failed · not scored", "provider"],
    transport_interrupted: ["infra", "Connection interrupted · not scored", "interrupted"],
    harness_error: ["infra", "Harness error · score unknown", "unknown"],
    scoring_interrupted: ["infra", "Scoring interrupted · score unknown", "unknown"],
    pending: ["infra", "No completed request evidence", "pending"],
    not_requested: ["infra", "Not requested", "skipped"],
  };
  const [cls, label, value] = states[run.outcome] || ["infra", "Outcome unavailable · score unknown", "unknown"];
  return {cls, label, value};
}
