/* Renders the leaderboard (index.html) and problem library (problems.html)
   from committed JSON under data/. The published records are the source of
   truth. No framework or build step. */
import { findExecution } from "./evidence.mjs";
import { parseCatalog } from "./catalog.mjs";
import { budgetSummary, outputLimit } from "./budget.mjs";
import { agentState } from "./outcomes.mjs";

// ---------------------------------------------------------------------------
// Page state
// ---------------------------------------------------------------------------

let data = null; // the loaded leaderboard: { meta, problems, models }
let datasets = {}; // dataset id -> path of its leaderboard.json
let datasetKey = ""; // the dataset being shown
let dialog = null; // the run-details <dialog>
let returnFocus = null; // element to refocus when the dialog closes
let scoreMetric = "beat_rate"; // ranking table: beat_rate | correctness_rate | geomean
let operatorMetric = "ratio"; // operator matrix: ratio | cells | cycles

const MODEL_NAMES = {
  "mimo-v2.5": "MiMo V2.5",
  "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
  "qwen3.8-flash": "Qwen3.8 Flash",
  "glm-5.3-flash": "GLM-5.3-Flash",
  "kimi-k2.6": "Kimi K2.6",
  "minimax-m2.7": "MiniMax M2.7",
};

// Provider logos, matched in order against the model id (the label without
// its provider prefix).
const LOGOS = [
  [/deepseek/i, "deepseek"],
  [/qwen|qwq/i, "qwen"],
  [/kimi|moonshot/i, "kimi"],
  [/minimax/i, "minimax"],
  [/glm|zhipu|z-?ai/i, "zai"],
  [/mimo|xiaomi/i, "xiaomi"],
  [/claude|anthropic/i, "claude"],
  [/gpt|openai|codex/i, "openai"],
  [/gemini|gemma/i, "gemini"],
  [/grok|^xai/i, "grok"],
  [/llama/i, "meta"],
  [/mistral|codestral|devstral|magistral/i, "mistral"],
  [/nemotron|nvidia/i, "nvidia"],
  [/doubao/i, "doubao"],
  [/hunyuan/i, "hunyuan"],
  [/stepfun|^step-/i, "stepfun"],
  [/cohere|^command/i, "cohere"],
  [/longcat/i, "longcat"],
  [/ernie/i, "ernie"],
  [/^yi-/i, "yi"],
  [/internlm|intern-s/i, "internlm"],
  [/^nova-|amazon/i, "nova"],
];

const SCORE_LABELS = {
  beat_rate: "Correct & better than baseline",
  correctness_rate: "Passed all correctness checks",
  geomean: "Geometric mean ADP ratio · correct runs only",
};
const SCORE_HEADINGS = {
  beat_rate: "Beat baseline",
  correctness_rate: "Correctness",
  geomean: "ADP gain",
};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

function formatInteger(n) {
  return Number.isFinite(n) && n >= 0 ? n.toLocaleString("en-US") : "—";
}

function formatPercent(n) {
  return Number.isFinite(n) ? `${Math.round(n * 100)}%` : "—";
}

function formatRatio(n) {
  return n > 0 ? `${n.toFixed(2)}×` : "—";
}

/** Set the text of every element matching `selector`. */
function fill(selector, text) {
  for (const element of $$(selector)) element.textContent = text;
}

/** "opencode-go/kimi-k2.6 [agent-assisted-v1]" -> "kimi-k2.6" */
function modelId(label) {
  return label.split("/").pop().replace(/\s*\[.*\]$/, "").replace(/-free$/, "");
}

function displayName(model) {
  const id = modelId(model.label);
  return MODEL_NAMES[id] || id;
}

function problemTitle(name) {
  return data.problems.find((problem) => problem.name === name)?.title || name;
}

/** Best first: beat rate, then correctness, then ADP gain, then name. */
function rankModels(models) {
  return [...models].sort(
    (a, b) =>
      b.beat_rate - a.beat_rate ||
      b.correctness_rate - a.correctness_rate ||
      b.geomean - a.geomean ||
      a.label.localeCompare(b.label),
  );
}

/** How a run is shown: CSS class, label, and short value. */
function runState(run) {
  if (run.correct) {
    if (run.ratio > 1) return { cls: "beat", label: "Beat baseline", value: formatRatio(run.ratio) };
    return { cls: "correct", label: "Correct, ≤ 1×", value: formatRatio(run.ratio) };
  }
  return agentState(run) || { cls: "fail", label: "Incorrect RTL", value: "wrong" };
}

// ---------------------------------------------------------------------------
// Models and reasoning
// ---------------------------------------------------------------------------

function modelIcon(model) {
  const id = modelId(model.label);
  const match = LOGOS.find(([pattern]) => pattern.test(id));
  if (match) return `<img src="assets/logos/${match[1]}.svg" alt="" width="15" height="15">`;
  return escapeHtml(displayName(model).slice(0, 1));
}

function modelButton(model) {
  const name = escapeHtml(displayName(model));
  return (
    `<button class="model-button" data-model="${escapeHtml(model.label)}" title="${escapeHtml(model.label)}" aria-label="Inspect ${name} runs">` +
    `<span class="model-icon" aria-hidden="true">${modelIcon(model)}</span>` +
    `<span class="model-label">${name}</span>` +
    `</button>`
  );
}

/** The reasoning setting the harness asked for, in words. */
function requestedReasoning(settings) {
  const thinking = settings.thinking || {};
  const parts = [];
  if (thinking.type === "disabled" || settings.reasoning?.enabled === false) {
    parts.push("off");
  } else if (thinking.type === "enabled" || settings.reasoning?.enabled === true) {
    parts.push(thinking.budget_tokens ? `on, ${formatInteger(thinking.budget_tokens)}-token budget` : "on");
  }
  if (settings.reasoning_effort) parts.push(`effort ${settings.reasoning_effort}`);
  return parts.join(" + ") || "provider default";
}

/** The share of a model's generated characters that were reasoning. */
function reasoningSummary(model) {
  const settings = model.runs.find((run) => run.generation?.generation_settings)?.generation.generation_settings;
  const measured = model.runs.map((run) => run.reasoning).filter(Boolean);
  const sum = (key) => measured.reduce((total, counts) => total + (Number(counts[key]) || 0), 0);
  const totalChars = sum("reasoning_chars") + sum("answer_chars");
  const share = measured.length && totalChars ? sum("reasoning_chars") / totalChars : null;
  const asked = settings ? requestedReasoning(settings) : "";

  let text = "—";
  let detail = "Reasoning was not measured for this model.";
  if (share !== null) {
    text = share > 0 ? formatPercent(share) : "None";
    detail =
      `${formatPercent(share)} of generated characters were reasoning, ` +
      `on ${formatInteger(sum("turns_with_reasoning"))} of ${formatInteger(sum("turns"))} turns.`;
  }
  return { text, asked, detail: asked ? `${detail} Requested: ${asked}.` : detail };
}

// ---------------------------------------------------------------------------
// Leaderboard page
// ---------------------------------------------------------------------------

function renderResults() {
  if (!$("#leaderboard-rows") || !data) return;
  const models = rankModels(data.models).sort((a, b) => b[scoreMetric] - a[scoreMetric]);
  renderRanking(models);
  renderOperatorMatrix(models);
  updateScrollHints();
}

/** Show a "scroll →" hint only under tables that are actually wider than the screen. */
function updateScrollHints() {
  for (const hint of $$(".scroll-hint")) {
    const table = hint.previousElementSibling;
    hint.classList.toggle("fits", table.scrollWidth <= table.clientWidth);
  }
}

function renderRanking(models) {
  const best = Math.max(0, ...models.map((model) => model[scoreMetric]));
  const isRate = scoreMetric !== "geomean";

  fill("#rank-metric-label", SCORE_HEADINGS[scoreMetric]);
  fill("#interval-label", isRate ? "" : "/ correct runs only");
  $("#chart-axis").innerHTML = [0, 0.25, 0.5, 0.75, 1]
    .map((tick) => `<span>${isRate ? formatPercent(tick) : `${(tick * best).toFixed(1)}×`}</span>`)
    .join("");
  fill(
    "#score-note",
    isRate
      ? "Rates use all scheduled slots. Unscored outcomes are not incorrect RTL; inspect each model."
      : "Geometric mean · correct runs only · higher is better.",
  );
  $("#leaderboard-rows").innerHTML = models.map((model) => rankingRow(model, best, isRate)).join("");
}

function rankingRow(model, best, isRate) {
  const value = model[scoreMetric];
  const reasoning = reasoningSummary(model);
  const text = isRate ? formatPercent(value) : formatRatio(value);

  let width = 0;
  if (isRate) width = value * 100;
  else if (best > 0) width = (Math.max(0, value) / best) * 100;

  let tooltip;
  if (isRate) {
    const unscored = model.unscored ? ` ${model.unscored} unscored; inspect typed outcomes.` : "";
    tooltip = `${SCORE_LABELS[scoreMetric]}: ${text}. ${model.correct}/${model.attempts} confirmed correct slots.${unscored}`;
  } else {
    tooltip = `${text} geometric mean across ${model.correct} correct attempts; ${model.attempts} total attempts.`;
  }

  const barClass = value === best && value > 0 ? "best" : "";
  const asked = reasoning.asked ? `<span>asked: ${escapeHtml(reasoning.asked)}</span>` : "";
  return (
    `<tr>` +
    `<th scope="row">${modelButton(model)}</th>` +
    `<td><button class="comparison-bar ${barClass}" data-model="${escapeHtml(model.label)}" title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(displayName(model))}: ${escapeHtml(tooltip)}">` +
    `<span class="comparison-track" aria-hidden="true"><span class="comparison-fill" style="width:${width}%"></span></span>` +
    `</button></td>` +
    `<td class="primary-stat"><strong>${text}</strong></td>` +
    `<td class="numeric-stat">${model.correct}/${model.attempts}</td>` +
    `<td class="numeric-stat">${formatRatio(model.geomean)}</td>` +
    `<td class="numeric-stat reasoning-stat" title="${escapeHtml(reasoning.detail)}"><strong>${reasoning.text}</strong>${asked}</td>` +
    `<td class="numeric-stat">${model.attempts}</td>` +
    `</tr>`
  );
}

function renderOperatorMatrix(models) {
  const bestByProblem = new Map(data.problems.map((problem) => [problem.name, bestValue(problem.name)]));
  const headings = data.problems
    .map((problem) => `<th scope="col"><a href="problems.html#${escapeHtml(problem.name)}">${escapeHtml(problem.title)}</a></th>`)
    .join("");
  const rows = models.map((model) => matrixRow(model, bestByProblem)).join("");

  $("#heatmap").innerHTML =
    `<table class="results-table matrix-table" style="--problems:${data.problems.length}">` +
    `<thead><tr><th scope="col">Model</th>${headings}<th scope="col" class="correct-heading">Correct</th></tr></thead>` +
    `<tbody>${rows}</tbody>` +
    `</table>`;

  let note = "ADP ratio = baseline / design · higher is better.";
  if (operatorMetric !== "ratio") {
    note = `${operatorMetric === "cells" ? "Cell count" : "Cycle count"} · lower is better.`;
  }
  fill("#matrix-note", note);
}

/** The best correct value of the operator metric on a problem, across all models (0 if none). */
function bestValue(problemName) {
  const values = data.models
    .flatMap((model) => model.runs)
    .filter((run) => run.problem === problemName && run.correct && run[operatorMetric] > 0)
    .map((run) => run[operatorMetric]);
  if (!values.length) return 0;
  return operatorMetric === "ratio" ? Math.max(...values) : Math.min(...values);
}

function matrixRow(model, bestByProblem) {
  const cells = data.problems
    .map((problem) => {
      const runs = model.runs
        .filter((run) => run.problem === problem.name)
        .sort((a, b) => a.attempt - b.attempt);
      if (!runs.length) return '<td><span class="result-bar none" aria-label="Not run">—</span></td>';
      const bars = runs.map((run) => resultBar(model, problem, run, runs.length, bestByProblem.get(problem.name)));
      return `<td>${bars.join("")}</td>`;
    })
    .join("");
  const correct = `${model.correct}/${model.attempts}`;
  return (
    `<tr><td>${modelButton(model)}</td>${cells}` +
    `<td><span class="correct-count" title="${correct} correct"><span>${correct}</span></span></td></tr>`
  );
}

function resultBar(model, problem, run, runCount, best) {
  const state = runState(run);
  const value = run[operatorMetric];
  const valid = run.correct && value > 0;
  const isBest = valid && value === best;

  let width = 0;
  if (valid && best > 0) width = (operatorMetric === "ratio" ? value / best : best / value) * 100;

  let text;
  if (valid) text = operatorMetric === "ratio" ? formatRatio(value) : formatInteger(value);
  else text = run.correct ? "—" : state.value;

  const score = valid ? text + " " + operatorMetric : "No valid score";
  const tooltip = `${displayName(model)} · ${problem.title} · attempt ${run.attempt}: ${state.label}. ${score}`;
  const attempt = runCount > 1 ? ` <small> · #${run.attempt}</small>` : "";
  const star = isBest ? '<span class="star" aria-hidden="true">★</span>' : "";
  return (
    `<button class="result-bar ${state.cls} ${isBest ? "best" : ""}" style="--fill:${width}%" ` +
    `data-model="${escapeHtml(model.label)}" data-problem="${escapeHtml(problem.name)}" data-attempt="${run.attempt}" ` +
    `title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(tooltip)}">${text}${attempt}${star}</button>`
  );
}

function renderMeta() {
  const meta = data.meta;
  fill("#meta-generated", meta.generated?.slice(0, 10) || "—");
  fill("#meta-commit", meta.git_commit?.slice(0, 10) || "—");
  fill("#attempt-heading", "Slots");
  for (const link of $$("a[data-results-download]")) link.href = datasets[datasetKey];
  fill(
    "#pilot-note",
    `Agent-assisted v1 · shared read/write/check/submit operations · up to ${meta.max_turns} model turns per slot · ` +
      `${budgetSummary(meta)} · development checks only during generation, then frozen held-out scoring. ` +
      "Incorrect RTL remains a failed measurement; provider, submission and interrupted outcomes are shown separately. " +
      "Rates use all scheduled slots, not only completed scores. " +
      "GitHub job success means evidence was recorded, not that the model passed.",
  );
}

// ---------------------------------------------------------------------------
// Run details dialog
// ---------------------------------------------------------------------------

function openRuns(label, problem, attempt) {
  const model = data.models.find((candidate) => candidate.label === label);
  if (!model) return;
  returnFocus = document.activeElement;
  const runs = model.runs.filter(
    (run) => (!problem || run.problem === problem) && (!attempt || run.attempt === attempt),
  );

  const summary =
    `<div class="dialog-summary">` +
    `<span class="badge">${model.beating}/${model.attempts} beat baseline</span>` +
    `<span class="badge">${model.correct}/${model.attempts} correct</span>` +
    `<span class="badge">${model.wrong_rtl} wrong RTL</span>` +
    `<span class="badge">${model.infra} infrastructure</span>` +
    `</div>`;
  let outcomes = "";
  if (model.outcomes) {
    const counts = Object.entries(model.outcomes)
      .map(([kind, count]) => `${escapeHtml(kind.replaceAll("_", " "))}: ${formatInteger(count)}`)
      .join(" · ");
    outcomes = `<p class="rc-meta">${formatInteger(model.scored)} scored · ${formatInteger(model.unscored)} unscored. ${counts}</p>`;
  }
  const reasoning = `<p class="rc-meta">${escapeHtml(model.label)}<br>Reasoning: ${escapeHtml(reasoningSummary(model).detail)}</p>`;

  dialog.innerHTML =
    `<div class="dialog-head">` +
    `<div><p class="eyebrow">FROZEN RUN RECORDS · ${escapeHtml(data.meta.pilot)}</p><h2 id="dialog-title">${escapeHtml(displayName(model))}</h2></div>` +
    `<button class="dialog-close" aria-label="Close run details" autofocus>×</button>` +
    `</div>` +
    `<div class="dialog-body">` +
    summary +
    outcomes +
    reasoning +
    `<div class="run-grid">${runs.map(runCard).join("")}</div>` +
    `<p class="pilot-note">Ratios are area–delay improvements, not clock-speed measurements. <a href="${datasets[datasetKey]}">Read the source records ↗</a></p>` +
    `</div>`;
  $(".dialog-close", dialog).addEventListener("click", () => dialog.close());
  dialog.showModal();
  document.body.style.overflow = "hidden";
}

function runCard(run) {
  const state = runState(run);
  const unknownScore = run.correct === null;
  const checkCount = run.generation?.dev_checks;

  let timing = "scoring details unavailable";
  if (!unknownScore) {
    const plural = checkCount === 1 ? "" : "s";
    const timedOut = run.timed_out ? " · agent timed out" : "";
    timing = `${formatInteger(Math.round(run.duration_s))}s · ${formatInteger(checkCount)} agent check${plural}${timedOut}`;
  }
  let audit = "unknown";
  if (!unknownScore) audit = run.audit_ok ? "passed" : "not passed";
  const details = [run.correctness, run.error]
    .filter(Boolean)
    .map((text) => `<p class="rc-detail">${escapeHtml(text)}</p>`)
    .join("");

  return (
    `<article class="run-card">` +
    `<div class="rc-head"><strong>${escapeHtml(problemTitle(run.problem))}</strong><span class="rc-status ${state.cls}">${state.label}</span></div>` +
    `<p class="rc-meta">Attempt ${run.attempt} · ${timing}</p>` +
    `<div class="rc-ratio">${run.correct ? formatRatio(run.ratio) : "—"}</div>` +
    `<p class="metric-caption">${run.correct ? "baseline ADP / design ADP" : "No valid ADP score"}</p>` +
    `<p class="rc-meta">${formatInteger(run.cells)} cells × ${formatInteger(run.cycles)} cycles<br>ADP ${formatInteger(run.adp)}<br>` +
    `Stage: ${escapeHtml(run.stage || "not reported")} · audit ${audit}</p>` +
    details +
    runEvidence(run) +
    `<details><summary>Inspect artifact hashes</summary>` +
    `<p>Submission SHA-256</p><p class="hash-value">${escapeHtml(run.submission_sha256 || "Not recorded")}</p>` +
    `<p>Netlist SHA-256</p><p class="hash-value">${escapeHtml(run.netlist_sha256 || "Not recorded")}</p>` +
    `</details>` +
    `</article>`
  );
}

/** Links to the original GitHub Actions evidence for an agent run. */
function runEvidence(run) {
  const execution = findExecution(run);
  if (!execution) return '<p class="rc-meta">Original Actions evidence unavailable.</p>';
  const generation = run.generation || {};
  const usage = generation.usage || {};
  const cap = outputLimit(data.meta, generation.model);
  const submission =
    generation.outcome === "submitted"
      ? "The explicit final submission was frozen for held-out scoring; inspect the outcome above to see whether scoring completed."
      : "No finalized submission was received; no held-out correctness score is claimed.";
  const tokens = formatInteger(usage.output_tokens ?? usage.completion_tokens);

  return (
    `<section class="run-evidence" aria-label="Original GitHub agent run">` +
    `<p class="evidence-title">Agent-assisted v1 · ${escapeHtml(run.outcome)}</p>` +
    `<p class="rc-meta">${formatInteger(generation.turns)} / ${formatInteger(generation.max_turns)} model turns · ` +
    `${formatInteger(generation.dev_checks)} development checks. ${submission} Development feedback is not a benchmark pass.</p>` +
    `<div class="evidence-links">` +
    `<a href="${escapeHtml(execution.job)}" target="_blank" rel="noopener">Scoring job &amp; logs ↗</a>` +
    `<a href="${escapeHtml(execution.workflow)}" target="_blank" rel="noopener">Generation jobs &amp; artifacts ↗</a>` +
    `<a href="${escapeHtml(execution.code)}" target="_blank" rel="noopener">Evaluated code ↗</a>` +
    `</div>` +
    `<p class="rc-meta">${generation.incomplete_usage ? "At least " : ""}${tokens} reported output tokens across turns. ` +
    `Requested limit: ${cap ? formatInteger(cap) : "unavailable"} tokens per response. ` +
    `Execution health: ${escapeHtml(run.execution_health)}.</p>` +
    `<details><summary>Generation settings</summary><p class="hash-value">${escapeHtml(JSON.stringify(generation.generation_settings || {}))}</p></details>` +
    `<p class="evidence-retention">Transcripts remain private Actions artifacts.</p>` +
    `</section>`
  );
}

// ---------------------------------------------------------------------------
// Problem library page
// ---------------------------------------------------------------------------

function renderProblems() {
  if (!$("#problem-grid")) return;
  $("#problem-grid").innerHTML = data.problems.map(problemCard).join("");
  openProblemFromHash();
  window.addEventListener("hashchange", openProblemFromHash);
}

/** Open (and scroll to) the problem named in the URL hash, if any. */
function openProblemFromHash() {
  let id;
  try {
    id = decodeURIComponent(location.hash.slice(1));
  } catch {
    return;
  }
  const target = document.getElementById(id);
  if (target?.classList.contains("problem-accordion")) {
    target.open = true;
    target.scrollIntoView();
  }
}

function problemCard(problem, index) {
  const baseline = problem.baseline || {};
  const sanity = problem.sanity || {};
  const source = `https://github.com/arygupt/ADPBench/blob/main/problems/level${problem.level}/${encodeURIComponent(problem.name)}`;
  const words = problem.out_len === 1 ? "" : "s";
  const params = Object.entries(problem.params || {})
    .map(([name, value]) => `<span class="chip">${escapeHtml(name)} ${escapeHtml(value)}</span>`)
    .join("");

  return (
    `<details class="problem-card problem-accordion" name="problems" id="${escapeHtml(problem.name)}">` +
    `<summary class="problem-toggle">` +
    `<span class="problem-number">${String(index + 1).padStart(2, "0")}</span>` +
    `<span class="problem-title">${escapeHtml(problem.title)}</span>` +
    `<span class="problem-chevron" aria-hidden="true">+</span>` +
    `</summary>` +
    `<div class="problem-content">` +
    `<p class="pc-desc">${problem.transactions} back-to-back transactions · ${problem.out_len} output word${words} per transaction. ` +
    `Exact integer arithmetic, with no reset between transactions.</p>` +
    `<div class="chip-row">${params}</div>` +
    `<div class="pc-metrics">` +
    `<div><strong>${formatInteger(baseline.cells)}</strong><span>baseline cells</span></div>` +
    `<div><strong>${formatInteger(baseline.cycles)}</strong><span>baseline cycles</span></div>` +
    `<div><strong>${formatRatio(sanity.correct ? sanity.ratio : 0)}</strong><span>sanity ADP improvement</span></div>` +
    `</div>` +
    `<p class="eyebrow">AREA–DELAY PRODUCT · LOWER IS BETTER</p>` +
    adpBars(baseline, sanity) +
    problemContract(problem) +
    `<div class="pc-links">` +
    `<a href="${source}/dut.py" target="_blank" rel="noopener">View executable spec ↗</a>` +
    `<a href="${source}/baseline.v" target="_blank" rel="noopener">Baseline RTL ↗</a>` +
    `</div>` +
    `</div>` +
    `</details>`
  );
}

/** Baseline vs sanity-solution ADP as two bars scaled to the larger one. */
function adpBars(baseline, sanity) {
  const max = Math.max(baseline.adp || 0, sanity.adp || 0);
  const bars = [
    ["Baseline", baseline.adp],
    ["Sanity", sanity.correct ? sanity.adp : null],
  ].map(([label, value]) => {
    const width = max > 0 && value > 0 ? (value / max) * 100 : 0;
    const color = label === "Sanity" ? "background:var(--muted)" : "";
    return (
      `<div class="bar-row"><span>${label}</span>` +
      `<span class="bar-track"><span class="bar-fill" style="width:${width}%;${color}"></span></span>` +
      `<span class="bar-num">${formatInteger(value)}</span></div>`
    );
  });
  return `<div class="bar-pair">${bars.join("")}</div>`;
}

/** Stream lengths, numeric contract, and directed edge cases. */
function problemContract(problem) {
  const streams = Object.entries(problem.input_lens || {})
    .map(([port, length]) => `<tr><th scope="row">${escapeHtml(port)}</th><td>${length} elements / transaction</td></tr>`)
    .join("");
  const quant = Object.entries(problem.quant || {})
    .map(([signal, contract]) => `<tr><th scope="row">${escapeHtml(signal)}</th><td>${escapeHtml(contract)}</td></tr>`)
    .join("");
  const directed = (problem.directed || []).map((name) => `<span class="chip">${escapeHtml(name)}</span>`).join("");
  return (
    `<details><summary>Interface, arithmetic & edge cases</summary>` +
    `<table class="contract-table"><tbody>${streams}${quant}</tbody></table>` +
    `<div class="chip-row">${directed}</div>` +
    `</details>`
  );
}

// ---------------------------------------------------------------------------
// Setup and loading
// ---------------------------------------------------------------------------

function setupShell() {
  dialog = document.createElement("dialog");
  dialog.setAttribute("aria-labelledby", "dialog-title");
  document.body.append(dialog);
  dialog.addEventListener("close", () => {
    document.body.style.overflow = "";
    returnFocus?.focus();
  });

  // Any element with data-model opens that model's runs (optionally one problem/attempt).
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-model]");
    if (button && data) {
      openRuns(button.dataset.model, button.dataset.problem, Number(button.dataset.attempt) || undefined);
    }
  });

  bindSegmentedControl("score", (value) => (scoreMetric = value));
  bindSegmentedControl("metric", (value) => (operatorMetric = value));
  window.addEventListener("resize", updateScrollHints);
}

/** Buttons with data-<name>="value": clicking one selects it and re-renders. */
function bindSegmentedControl(name, select) {
  const buttons = $$(`[data-${name}]`);
  for (const button of buttons) {
    button.addEventListener("click", () => {
      select(button.dataset[name]);
      for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
      renderResults();
    });
  }
}

async function getJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`Could not load ${path} (${response.status})`);
  return response.json();
}

async function loadData() {
  const catalog = parseCatalog(await getJSON("data/evaluations.json"));
  datasets = catalog.paths;
  // Show the newest agent-assisted batch; the publisher prepends new batches.
  const agentDataset = catalog.entries.find((entry) => entry.protocol.startsWith("agent-assisted-"));
  datasetKey = agentDataset?.id || catalog.defaultId;

  const leaderboardPath = datasets[datasetKey];
  const [leaderboard, problems, reasoningFile] = await Promise.all([
    getJSON(leaderboardPath),
    getJSON("data/problems.json").catch(() => null),
    // Reasoning counts backfilled for batches published before receipts recorded them.
    getJSON(leaderboardPath.replace(/leaderboard\.json$/, "reasoning.json")).catch(() => null),
  ]);
  if (!Array.isArray(leaderboard.models) || !Array.isArray(problems?.problems || leaderboard.problems)) {
    throw new Error("Invalid published dataset");
  }

  const onProblemPage = Boolean($("#problem-grid"));
  data = {
    ...leaderboard,
    meta: leaderboard.meta || {},
    problems: onProblemPage ? problems?.problems || leaderboard.problems : leaderboard.problems,
  };
  const backfill = reasoningFile?.schema_version === 1 ? reasoningFile.runs || {} : {};
  for (const model of data.models) {
    for (const run of model.runs) {
      run.reasoning =
        run.generation?.reasoning_measured || backfill[`${modelId(model.label)}/${run.problem}`] || null;
    }
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  setupShell();
  try {
    await loadData();
    renderResults();
    renderProblems();
    renderMeta();
  } catch (error) {
    $("#main").insertAdjacentHTML(
      "afterbegin",
      '<div class="error-state" role="alert"><strong>Published results could not load.</strong><p>Refresh to retry.</p></div>',
    );
    for (const element of $$(".loading")) element.remove();
    console.error(error);
  }
});
