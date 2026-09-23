import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import test from "node:test";
import { findReplay, findExecution } from "../site/assets/evidence.mjs";
import { parseCatalog } from "../site/assets/catalog.mjs";
import { budgetSummary, outputLimit } from "../site/assets/budget.mjs";

const readJSON = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const leaderboard = readJSON("../site/data/leaderboard.json");
const evidence = readJSON("../site/data/evidence.json");
const runs = leaderboard.models.flatMap((m) => m.runs);
const pilot = leaderboard.meta.pilot;
const first = evidence.runs[0];
const run = runs.find((r) => r.submission_sha256 === first.submission_sha256);

test("compatibility panel shows frozen diagnostic evidence without changing benchmark outcomes", () => {
  const html = readFileSync(new URL("../site/index.html", import.meta.url), "utf8");
  const panel = html.match(/<section class="compatibility-panel"[\s\S]*?<\/section>/)?.[0];
  assert.ok(panel);
  const receipt = readJSON("../pilot/diagnostics/go-canary-followup-20260923.json");
  assert.equal(receipt.benchmark_results, false);
  assert.equal(receipt.source_conclusion, "success");
  assert.match(panel, /Not benchmark scores/);
  assert.match(panel, /Historical benchmark outcomes are unchanged/);
  assert.match(panel, /batch is authorized with provider-maximum output limits/);
  assert.equal(readJSON("../pilot/go-core-provider-max-20260923.json").generation_enabled, true);
  assert.equal(readJSON("../pilot/go-core-provider-max-20260922.json").generation_enabled, false);
  const rows = [...panel.matchAll(/<li data-compatibility-model="([a-z0-9.-]+)">([\s\S]*?)<\/li>/g)];
  assert.deepEqual(rows.map(r => r[1]), receipt.models.map(m => m.model));
  for (const [index, row] of rows.entries()) {
    const model = receipt.models[index];
    assert.equal(model.status, "completed");
    assert.equal(model.xor_input_combinations_passed, 4);
    assert.equal(model.yosys_synthesis_check, "passed");
    assert.equal(model.iverilog_compile, "passed");
    assert.ok(row[2].includes(`Verified XOR · ${model.output_tokens} output tokens`));
    const source = readFileSync(new URL(`../${model.rtl_file}`, import.meta.url));
    assert.equal(createHash("sha256").update(source).digest("hex"), model.rtl_sha256);
  }
  const links = Object.fromEntries([...panel.matchAll(/data-compatibility-link="([a-z]+)" href="([^"]+)"/g)].map(m => [m[1],m[2]]));
  assert.equal(links.run, receipt.source_run_url);
  assert.equal(links.verification, "https://github.com/arygupt/ADPBench/actions/runs/35818209664");
  assert.equal(links.report, "https://github.com/arygupt/ADPBench/blob/5e01b572e384ae396225975c27a924226591d2e7/pilot/diagnostics/go-canary-followup-20260923.md");
  assert.equal(links.original, readJSON("../pilot/diagnostics/go-canary-20260923.json").source_run_url);
  const go = readJSON("../site/data/go-core-20260922/leaderboard.json");
  assert.equal(go.models.flatMap(m => m.runs).length, 12);
  assert.equal(go.models.reduce((sum,m) => sum+m.correct, 0), 0);
});

test("old fixed and new model-maximum budgets are displayed without claiming unlimited output", () => {
  const old = readJSON("../site/data/go-core-20260922/leaderboard.json");
  assert.equal(budgetSummary(old.meta), "8,192 output-token cap/request");
  assert.equal(outputLimit(old.meta, "mimo-v2.5"), 8192);
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
  assert.equal(parsed.paths["pilot-001"], "data/leaderboard.json");
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
    assert.equal(receipt.source_workflow, ".github/workflows/go-core.yml");
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
    for (const model of plan.models) {
      assert.match(model.id, /^[a-z0-9][a-z0-9.-]*$/);
      const published = board.models.find(m => m.label === `opencode-go/${model.id} [single-shot]`);
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
        assert.equal(shown.correct, Boolean(record.result?.correct));
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
      assert.ok(plan.models.some(m => ["go-core", "go-generation"].some(prefix => artifact.name === `${prefix}-${m.id}-${receipt.source_run_id}`)));
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
