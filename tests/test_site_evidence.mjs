import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import test from "node:test";
import { findExecution } from "../site/assets/evidence.mjs";
import { parseCatalog } from "../site/assets/catalog.mjs";
import { budgetSummary, outputLimit } from "../site/assets/budget.mjs";
import { agentState } from "../site/assets/outcomes.mjs";

const readJSON = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));

test("site typography uses medium body and semibold emphasis without shorthand resets", () => {
  const css = readFileSync(new URL("../site/assets/style.css", import.meta.url), "utf8");
  assert.match(css, /--weight-normal:\s*500;/);
  assert.match(css, /--weight-strong:\s*600;/);
  assert.match(css, /body\s*\{[^}]*font:\s*var\(--weight-normal\)\s+14px\/1\.5\s+var\(--font\);/);
  assert.match(css, /\.ranking-table tbody td\s*\{[^}]*font-weight:\s*var\(--weight-normal\);/);
  assert.match(css, /\.primary-stat strong\s*\{[^}]*font-weight:\s*var\(--weight-strong\);/);
  assert.doesNotMatch(css, /font-weight:\s*400\s*;/);
  for (const [, shorthand] of css.matchAll(/(?:^|[;{\n])\s*font:\s*([^;]+);/g))
    assert.ok(shorthand === "inherit" || /^var\(--weight-(?:normal|strong)\)\s/.test(shorthand), shorthand);
});

test("agent outcomes distinguish wrong RTL from unscored execution and submission failures", () => {
  const generation = {protocol:"agent-assisted-v1"};
  assert.equal(agentState({generation, outcome:"incorrect"}).value, "wrong");
  assert.equal(agentState({generation, outcome:"scoring_interrupted", correct:null}).value, "unknown");
  assert.equal(agentState({generation, outcome:"invalid_submission"}).value, "invalid");
  assert.equal(agentState({generation, outcome:"provider_error"}).cls, "infra");
  assert.equal(agentState({generation, outcome:"turn_limit"}).cls, "fail");
  assert.equal(agentState({generation, outcome:"<script>"}).value, "unknown");
  assert.equal(agentState({generation, outcome:"quota_exhausted"}).cls, "infra");
  assert.equal(agentState({generation:{protocol:"agent-assisted-v2"}, outcome:"quota_exhausted"}).value, "quota");
  assert.equal(agentState({generation:{protocol:"single-shot"}}), null);
  for (const protocol of ["agent-assisted-v1", "agent-assisted-v2"]) {
    const catalog = {schema_version:1, default:"agent-test", evaluations:[{
      id:"agent-test", label:"Agent-assisted", path:"data/agent-test/leaderboard.json", protocol}]};
    assert.equal(parseCatalog(catalog).defaultId, "agent-test");
  }
  assert.throws(() => parseCatalog({schema_version:1, default:"agent-test", evaluations:[{
    id:"agent-test", label:"Agent-assisted", path:"data/agent-test/leaderboard.json", protocol:"agent-assisted-v9"}]}));
});

test("old fixed and new model-maximum budgets are displayed without claiming unlimited output", () => {
  const old = readJSON("../pilot/go-core-20260922.json");
  assert.equal(budgetSummary(old), "8,192 output-token cap/request");
  assert.equal(outputLimit(old, "mimo-v2.5"), 8192);
  const plan = readJSON("../pilot/go-core-provider-max-20260922.json");
  assert.equal(outputLimit(plan, "kimi-k2.6"), 65536);
  assert.equal(outputLimit(plan, "deepseek-v4.1-flash"), 384000);
  assert.equal(outputLimit(plan, "unknown"), null);
  assert.match(budgetSummary(plan), /65,536–384,000/);
  assert.doesNotMatch(budgetSummary(plan), /unlimited/i);
  assert.equal(budgetSummary({output_budget:"provider_max",max_output_tokens:{bad:"unlimited"}}), "Output limits unavailable");
});

test("catalog only permits unique local datasets and existing default", () => {
  const catalog = readJSON("../site/data/evaluations.json");
  const parsed = parseCatalog(catalog);
  assert.equal(parsed.paths[catalog.default], `data/${catalog.default}/leaderboard.json`);
  for (const entry of catalog.evaluations) {
    const board = readJSON(`../site/${entry.path}`);
    assert.equal(board.meta.pilot, entry.id);
  }
  for (const delta of [{default:"missing"}, {evaluations:[...catalog.evaluations, catalog.evaluations[0]]},
                       {evaluations:[{...catalog.evaluations[0], path:"https://evil.invalid"}]},
                       {evaluations:[{...catalog.evaluations[0], id:"../escape"}]}])
    assert.throws(() => parseCatalog({...catalog, ...delta}));
});

test("every publication receipt binds exact frozen records and website outcomes", () => {
  const directory = new URL("../pilot/publications/", import.meta.url);
  const files = existsSync(directory) ? readdirSync(directory) : [];
  for (const file of files) {
    assert.match(file, /^run-[1-9][0-9]*-attempt-[1-9][0-9]*\.json$/);
    const receipt = JSON.parse(readFileSync(new URL(file, directory), "utf8"));
    assert.equal(receipt.schema_version, 1);
    assert.equal(receipt.kind, "validated-model-results-publication");
    for (const id of [receipt.source_run_id, receipt.source_attempt])
      assert.ok(Number.isSafeInteger(id) && id > 0);
    assert.equal(file, `run-${receipt.source_run_id}-attempt-${receipt.source_attempt}.json`);
    assert.match(receipt.source_commit, /^[0-9a-f]{40}$/);
    assert.ok([".github/workflows/go-core.yml", ".github/workflows/go-agent.yml"].includes(receipt.source_workflow));
    assert.match(receipt.dataset, /^[a-z0-9][a-z0-9-]{0,79}$/);
    const root = `../pilot/results/${receipt.dataset}`;
    const hashes = receipt.record_file_sha256;
    const expectedPaths = ["plan.json"];
    const board = readJSON(`../site/data/${receipt.dataset}/leaderboard.json`);
    assert.equal(board.meta.pilot, receipt.dataset);
    assert.equal(board.meta.git_commit, receipt.source_commit);
    assert.equal(receipt.confirmed_correct, board.models.reduce((n, m) => n + m.correct, 0));
    assert.equal(receipt.result_slots, board.models.reduce((n, m) => n + m.runs.length, 0));
    const plan = readJSON(`${root}/plan.json`);
    const protocol = plan.protocol || "single-shot";
    for (const model of plan.models) {
      assert.match(model.id, /^[a-z0-9][a-z0-9.-]*$/);
      const published = board.models.find(m => m.label === `opencode-go/${model.id} [${protocol}]`);
      assert.ok(published);
      for (const problem of plan.problems) {
        assert.match(problem, /^[a-z0-9_]+$/);
        const base = `opencode-go-${model.id}/${problem}`;
        for (const filename of ["generation.json", "manifest.json", "record.json"])
          expectedPaths.push(`${base}/rep1/${filename}`);
        const record = readJSON(`${root}/${base}/rep1/record.json`);
        const shown = published.runs.find(r => r.problem === problem);
        assert.ok(findExecution(shown));
        assert.deepEqual(shown.execution, record.execution);
        assert.equal(record.execution.run_id, receipt.source_run_id);
        assert.equal(record.execution.run_attempt, receipt.source_attempt);
        assert.equal(record.execution.commit, receipt.source_commit);
        assert.equal(shown.correct, protocol === "agent-assisted-v1" && !["correct", "incorrect"].includes(record.outcome) ? null : Boolean(record.result?.correct));
        for (const metric of ["cells", "cycles"])
          assert.equal(shown[metric], record.result?.[metric] ?? -1);
        assert.equal(shown.ratio, record.result?.ratio || -1);
        if (record.manifest.submission_sha256) {
          expectedPaths.push(`${base}/rep1_frozen/dut.v`);
          assert.equal(hashes[`${base}/rep1_frozen/dut.v`], record.manifest.submission_sha256);
          assert.equal(shown.submission_sha256, record.manifest.submission_sha256);
        }
      }
    }
    // Only canonical planned paths can be read; never arbitrary receipt paths.
    assert.deepEqual(Object.keys(hashes).sort(), expectedPaths.sort());
    for (const path of expectedPaths) {
      assert.match(hashes[path], /^[0-9a-f]{64}$/);
      assert.equal(createHash("sha256").update(readFileSync(new URL(`${root}/${path}`, import.meta.url))).digest("hex"), hashes[path]);
    }
    const names = new Set();
    for (const artifact of receipt.artifacts) {
      assert.ok(Number.isSafeInteger(artifact.id) && artifact.id > 0);
      assert.match(artifact.digest, /^sha256:[0-9a-f]{64}$/);
      assert.ok(plan.models.some(m => protocol === "agent-assisted-v1"
        ? plan.problems.some(p => ["go-agent-records", "go-agent-generation"].some(prefix => artifact.name === `${prefix}-${m.id}-${p}-${receipt.source_run_id}`))
        : ["go-core", "go-generation"].some(prefix => artifact.name === `${prefix}-${m.id}-${receipt.source_run_id}`)));
      assert.ok(!names.has(artifact.name));
      names.add(artifact.name);
    }
  }
});

test("original model jobs remain distinct from verified replays", () => {
  const r = { execution: { kind: "model-evaluation", repository: "arygupt/ADPBench", run_id: 123, run_attempt: 1, job_id: 456, commit: "a".repeat(40), completed_at: "2026-09-22T01:00:00Z", job_conclusion: "failure" } };
  assert.equal(findExecution(r).job, "https://github.com/arygupt/ADPBench/actions/runs/123/job/456");
  for (const delta of [{kind:"replay"}, {repository:"evil/repo"}, {run_id:"123"}, {job_id:0}, {commit:"main"}, {job_conclusion:"skipped"}, {completed_at:"bad"}])
    assert.equal(findExecution({ execution: {...r.execution, ...delta} }), null);
  assert.equal(findExecution({}), null);
});

