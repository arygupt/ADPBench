import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { findReplay } from "../site/assets/evidence.mjs";

const readJSON = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const leaderboard = readJSON("../site/data/leaderboard.json");
const evidence = readJSON("../site/data/evidence.json");
const runs = leaderboard.models.flatMap((m) => m.runs);
const pilot = leaderboard.meta.pilot;
const first = evidence.runs[0];
const run = runs.find((r) => r.submission_sha256 === first.submission_sha256);

test("six exact frozen submissions have replay links; scores and attempts are untouched", () => {
  const before = JSON.stringify(leaderboard);
  assert.equal(runs.length, 20);
  assert.equal(evidence.runs.length, 6);
  assert.equal(runs.filter((r) => findReplay(evidence, pilot, r)).length, 6);
  for (const entry of evidence.runs) {
    const matches = runs.filter((r) => r.label === entry.label && r.problem === entry.problem && r.attempt === entry.attempt);
    assert.equal(matches.length, 1);
    const replay = findReplay(evidence, pilot, matches[0]);
    assert.ok(replay);
    assert.equal(replay.job, `https://github.com/arygupt/ADPBench/actions/runs/35528582943/job/${entry.replay.job_id}`);
    assert.ok(replay.workflow.endsWith("/attempts/1"));
    assert.ok(replay.source.includes(`/blob/${entry.replay.commit}/`));
    const source = readFileSync(new URL(`../${entry.replay.submission_path}`, import.meta.url));
    assert.equal(createHash("sha256").update(source).digest("hex"), entry.submission_sha256);
  }
  assert.equal(JSON.stringify(leaderboard), before);
});

test("missing, malformed and other-pilot evidence do not imply verification", () => {
  for (const value of [null, {}, { schema_version: 1, pilot, runs: [null] }, { ...evidence, schema_version: 2 }, { ...evidence, pilot: "different-pilot" }])
    assert.equal(findReplay(value, pilot, run), null);
});

test("identity, hash and recorded metrics must all match", () => {
  for (const delta of [
    { label: "other-model" }, { problem: "other-problem" }, { attempt: 2 },
    { submission_sha256: "a".repeat(64) }, { submission_sha256: "" },
    { correct: false }, { cells: run.cells + 1 }, { cycles: run.cycles + 1 }, { ratio: run.ratio + 1 },
  ]) assert.equal(findReplay(evidence, pilot, { ...run, ...delta }), null);
});

test("unsafe or unverified replay metadata cannot become links", () => {
  for (const delta of [
    { match: false }, { repository: "evil.example/repo/path" }, { repository: "../repo" },
    { run_id: "35528582943" }, { job_id: 0 }, { run_attempt: -1 },
    { commit: "main" }, { submission_path: "../../secret.v" },
    { completed_at: "<script>" },
  ]) {
    const changed = { ...evidence, runs: [{ ...first, replay: { ...first.replay, ...delta } }] };
    assert.equal(findReplay(changed, pilot, run), null);
  }
});
