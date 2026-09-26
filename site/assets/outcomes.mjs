// How agent-assisted runs are shown. Typed outcomes are independent of the
// GitHub job's color, and an unknown score is never shown as a failure.

// outcome -> [CSS class, label, short value]
const STATES = {
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
const UNKNOWN = ["infra", "Outcome unavailable · score unknown", "unknown"];

/** { cls, label, value } for an agent-assisted run, or null for other protocols. */
export function agentState(run) {
  if (run?.generation?.protocol !== "agent-assisted-v1") return null;
  const [cls, label, value] = Object.hasOwn(STATES, run.outcome) ? STATES[run.outcome] : UNKNOWN;
  return { cls, label, value };
}
