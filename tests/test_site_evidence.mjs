import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { findReplay, findExecution } from "../site/assets/evidence.mjs";

const readJSON = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const leaderboard = readJSON("../site/data/leaderboard.json");
const evidence = readJSON("../site/data/evidence.json");
const runs = leaderboard.models.flatMap((m) => m.runs);
const pilot = leaderboard.meta.pilot;
const first = evidence.runs[0];
const run = runs.find((r) => r.submission_sha256 === first.submission_sha256);

test("original model jobs remain distinct from verified replays", () => {
  const r = { execution: { kind: "model-evaluation", repository: "arygupt/ADPBench", run_id: 123, run_attempt: 1, job_id: 456, commit: "a".repeat(40), completed_at: "2026-09-22T01:00:00Z", job_conclusion: "failure" } };
  assert.equal(findExecution(r).job, "https://github.com/arygupt/ADPBench/actions/runs/123/job/456");
  for (const delta of [{kind:"replay"}, {repository:"evil/repo"}, {run_id:"123"}, {job_id:0}, {commit:"main"}, {job_conclusion:"skipped"}, {completed_at:"bad"}])
    assert.equal(findExecution({ execution: {...r.execution, ...delta} }), null);
  assert.equal(findExecution({}), null);
  assert.equal(findReplay(evidence, pilot, r), null);
});

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

test("all six Go models publish exact original Actions outcomes, including failures", () => {
  const go = readJSON("../site/data/go-core-20260922/leaderboard.json");
  assert.equal(go.meta.protocol, "single-shot");
  assert.equal(go.problems.length, 2);
  assert.equal(go.models.length, 6);
  const expected = ["mimo-v2.5", "deepseek-v4.1-flash", "qwen3.8-flash", "glm-5.3-flash", "kimi-k2.6", "minimax-m2.7"].sort();
  const ids = go.models.map(m => m.label.replace("opencode-go/", "").replace(" [single-shot]", ""));
  assert.deepEqual([...ids].sort(), expected);
  assert.equal(go.models.flatMap(m => m.runs).length, 12);
  for (const model of go.models) {
    const id = model.label.replace("opencode-go/", "").replace(" [single-shot]", "");
    assert.equal(model.runs.length, 2);
    for (const run of model.runs) {
      const execution = findExecution(run);
      assert.ok(execution);
      assert.equal(run.execution.commit, go.meta.git_commit);
      assert.equal(findReplay(evidence, go.meta.pilot, run), null);
      const base = `../pilot/results/go-core-20260922/opencode-go-${id}/${run.problem}`;
      const record = readJSON(`${base}/rep1/record.json`);
      assert.equal(run.correct, Boolean(record.result?.correct));
      assert.equal(run.cells, record.result?.cells ?? -1);
      assert.equal(run.cycles, record.result?.cycles ?? -1);
      assert.equal(run.ratio, record.result?.ratio || -1);
      assert.deepEqual(run.execution, record.execution);
      assert.deepEqual(run.generation.usage, record.manifest.generation.usage ?? null);
      if (run.submission_sha256) {
        const rtl = readFileSync(new URL(`${base}/rep1_frozen/dut.v`, import.meta.url));
        assert.equal(createHash("sha256").update(rtl).digest("hex"), run.submission_sha256);
      } else assert.equal(run.correct, false);
    }
  }
});
