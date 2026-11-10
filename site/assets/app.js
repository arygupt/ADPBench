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
let catalogProblems = []; // every problem in the library, run or not
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
  "qwen3.8-max": "Qwen3.8 Max",
  "grok-4.7": "Grok 4.7",
  "kimi-k3": "Kimi K3",
  "gpt-6-luna": "GPT-6 Luna",
  "deepseek-v4-pro": "DeepSeek V4 Pro",
  "glm-5.3": "GLM-5.3",
  "mimo-v2.6-pro": "MiMo V2.6 Pro",
  "minimax-m3": "MiniMax M3",
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

/** One colour per model maker, keyed by logo name. */
const MAKERS = {
  deepseek: ["DeepSeek", "#6f8dfa"],
  qwen: ["Qwen", "#a98bf5"],
  kimi: ["Kimi", "#4ec9b4"],
  minimax: ["MiniMax", "#f0668a"],
  zai: ["GLM", "#e3bd4f"],
  xiaomi: ["MiMo", "#f97316"],
  claude: ["Claude", "#e0785a"],
  openai: ["GPT", "#7ccf8a"],
  gemini: ["Gemini", "#5fb2f2"],
  grok: ["Grok", "#d6d3d1"],
};
const OTHER_MAKER = ["Other", "#a8a29e"];

function maker(model) {
  const id = modelId(model.label);
  const match = LOGOS.find(([pattern]) => pattern.test(id));
  return (match && MAKERS[match[1]]) || OTHER_MAKER;
}


const SCORE_LABELS = {
  beat_rate: "Correct & better than baseline",
  correctness_rate: "Passed all correctness checks",
  geomean: "Geometric mean ADP ratio, correct runs only",
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
  return agentState(run) || { cls: "fail wrong", label: "Incorrect RTL", value: "wrong" };
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

/** "agent-assisted-v1" -> "Agent-assisted v1" */
function protocolName(protocol) {
  const match = /^(.*)-(v\d+)$/.exec(protocol || "");
  const name = match ? `${match[1]} ${match[2]}` : protocol || "";
  return name.charAt(0).toUpperCase() + name.slice(1);
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
  if (settings.reasoning_effort) parts.push(`${settings.reasoning_effort} effort`);
  return parts.join(", ") || "provider default";
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

function renderResults(updateRanking = true) {
  if (!$("#leaderboard-rows") || !data) return;
  const models = rankModels(data.models).sort((a, b) => b[scoreMetric] - a[scoreMetric]);
  if (updateRanking) renderRanking(models);
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

const SCORE_NOTES = {
  beat_rate: "Share of runs that are correct and beat the baseline.",
  correctness_rate: "Share of runs that pass every check.",
  geomean: "Mean ADP improvement over correct runs.",
};

function renderRanking(models) {
  const best = Math.max(0, ...models.map((model) => model[scoreMetric]));
  const isRate = scoreMetric !== "geomean";

  fill("#score-note", SCORE_NOTES[scoreMetric]);
  $("#slot-key").hidden = !isRate;
  $("#leaderboard-rows").innerHTML = models.map((model, index) => rankingRow(model, index, best, isRate)).join("");

  const makers = new Map(models.map((model) => maker(model)));
  $("#vendor-key").innerHTML = [...makers]
    .map(([name, color]) => `<li><i class="maker" style="background:${color}"></i>${escapeHtml(name)}</li>`)
    .join("");
}

/** One mark per slot, in problem order, so a rate shows which slots it is made of. */
function slotMarks(model) {
  const order = new Map(data.problems.map((problem, index) => [problem.name, index]));
  return [...model.runs]
    .sort((a, b) => (order.get(a.problem) ?? 99) - (order.get(b.problem) ?? 99) || a.attempt - b.attempt)
    .map((run) => {
      const state = runState(run);
      // In the correctness view a correct slot is a full mark whether or not it beat the baseline.
      const cls = scoreMetric === "correctness_rate" && run.correct ? "beat" : state.cls;
      return `<i class="slot ${cls}"></i>`;
    })
    .join("");
}

function rankingRow(model, index, best, isRate) {
  const value = model[scoreMetric];
  const text = isRate ? formatPercent(value) : formatRatio(value);

  let tooltip;
  if (isRate) {
    const unscored = model.unscored ? ` ${model.unscored} unscored; inspect typed outcomes.` : "";
    tooltip = `${SCORE_LABELS[scoreMetric]}: ${text}. ${model.correct}/${model.attempts} confirmed correct slots.${unscored}`;
  } else {
    tooltip = `${text} geometric mean across ${model.correct} correct attempts; ${model.attempts} total attempts.`;
  }

  let chart;
  if (isRate) {
    chart = `<span class="slots" aria-hidden="true">${slotMarks(model)}</span>`;
  } else {
    const width = best > 0 ? (Math.max(0, value) / best) * 100 : 0;
    chart = `<span class="comparison-track" aria-hidden="true"><span class="comparison-fill" style="width:${width}%"></span></span>`;
  }

  let detail = `${model.correct}/${model.attempts} correct`;
  if (isRate) detail = model.geomean > 0 ? `${formatRatio(model.geomean)} gain` : "";
  const rank = index + 1;
  return (
    `<li class="board-row" data-row-model="${escapeHtml(model.label)}" style="--vendor:${maker(model)[1]}">` +
    `<span class="rank-badge${rank <= 3 ? ` top r${rank}` : ""}">${rank}</span>` +
    `<button class="board-open" data-model="${escapeHtml(model.label)}" title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(displayName(model))}: ${escapeHtml(tooltip)}">` +
    `<span class="board-line"><span class="board-name">${escapeHtml(displayName(model))}</span>` +
    `<span class="board-detail">${detail}</span>` +
    `<span class="primary-stat"><strong>${text}</strong></span></span>` +
    chart +
    `</button></li>`
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

  let note = "Baseline ADP ÷ design ADP for each run. Higher is better.";
  if (operatorMetric !== "ratio") {
    note = `${operatorMetric === "cells" ? "Gate cells after synthesis" : "Clock cycles to finish the test transactions"} for each run. Lower is better.`;
  }
  fill("#matrix-note", note);
  const unrun = unrunProblems().map((problem) => problem.title);
  fill("#unrun-note", unrun.length ? `Not part of this round: ${unrun.join(", ")}.` : "");
}

/** Library problems this dataset did not include. */
function unrunProblems() {
  const shown = new Set(data.problems.map((problem) => problem.name));
  return catalogProblems.filter((problem) => !shown.has(problem.name));
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

  let strength = 0;
  if (valid && best > 0) strength = operatorMetric === "ratio" ? value / best : best / value;

  let text;
  if (valid) text = operatorMetric === "ratio" ? formatRatio(value) : formatInteger(value);
  else text = run.correct ? "—" : state.value;

  const score = valid ? text + " " + operatorMetric : "No valid score";
  const tooltip = `${displayName(model)}, ${problem.title}, attempt ${run.attempt}: ${state.label}. ${score}`;
  const attempt = runCount > 1 ? ` <small>#${run.attempt}</small>` : "";
  return (
    `<button class="result-bar ${state.cls} ${isBest ? "best" : ""}" style="--strength:${strength.toFixed(3)}" ` +
    `data-model="${escapeHtml(model.label)}" data-problem="${escapeHtml(problem.name)}" data-attempt="${run.attempt}" ` +
    `title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(tooltip)}">${text}${attempt}</button>`
  );
}

// ---------------------------------------------------------------------------
// Area–delay plane: every correct design relative to its problem's baseline
// ---------------------------------------------------------------------------

// One marker outline per problem, drawn around the origin at radius r.
const MARKERS = [
  (r) => `M${-r},0a${r},${r} 0 1,0 ${2 * r},0a${r},${r} 0 1,0 ${-2 * r},0`,
  (r) => `M${-r * 0.88},${-r * 0.88}h${r * 1.76}v${r * 1.76}h${-r * 1.76}z`,
  (r) => `M0,${-r * 1.25}L${r * 1.25},0L0,${r * 1.25}L${-r * 1.25},0z`,
  (r) => `M0,${-r * 1.2}L${r * 1.15},${r * 0.85}L${-r * 1.15},${r * 0.85}z`,
  (r) => `M0,${r * 1.2}L${r * 1.15},${-r * 0.85}L${-r * 1.15},${-r * 0.85}z`,
  (r) => `M${-r * 1.2},${-r * 0.4}h${r * 0.8}v${-r * 0.8}h${r * 0.8}v${r * 0.8}h${r * 0.8}v${r * 0.8}h${-r * 0.8}v${r * 0.8}h${-r * 0.8}v${-r * 0.8}h${-r * 0.8}z`,
  (r) => `M${-r * 1.2},${-r * 0.6}h${r * 2.4}v${r * 1.2}h${-r * 2.4}z`,
  (r) => `M${-r * 0.6},${-r * 1.2}h${r * 1.2}v${r * 2.4}h${-r * 1.2}z`,
];
let planePoints = [];

/** "1/32×", "1×", "8×" for a power of two. */
function powerLabel(exponent) {
  return exponent < 0 ? `1/${2 ** -exponent}×` : `${2 ** exponent}×`;
}

function planeData() {
  const points = [];
  data.problems.forEach((problem, shape) => {
    const base = problem.baseline;
    if (!(base?.cells > 0 && base?.cycles > 0)) return;
    for (const model of data.models) {
      for (const run of model.runs) {
        if (run.problem !== problem.name || !run.correct || !(run.cells > 0 && run.cycles > 0)) continue;
        points.push({ model, problem, run, shape, x: Math.log2(run.cycles / base.cycles), y: Math.log2(run.cells / base.cells) });
      }
    }
  });
  return points;
}

function renderPlane() {
  const host = $("#plane");
  if (!host || !data) return;
  planePoints = planeData();
  $("#plane-figure").hidden = !planePoints.length;
  if (!planePoints.length) return;

  const width = Math.max(300, host.clientWidth);
  const height = Math.round(Math.min(520, Math.max(300, width * 0.68)));
  const pad = { left: 52, right: 14, top: 12, bottom: 44 };
  const xs = planePoints.map((point) => point.x);
  const ys = planePoints.map((point) => point.y);
  const x0 = Math.floor(Math.min(...xs)) - 0.45;
  const x1 = Math.max(0, Math.ceil(Math.max(...xs))) + 0.55;
  const y0 = Math.min(0, Math.floor(Math.min(...ys))) - 0.55;
  const y1 = Math.max(1, Math.ceil(Math.max(...ys))) + 0.3;
  const px = (x) => pad.left + ((x - x0) / (x1 - x0)) * (width - pad.left - pad.right);
  const py = (y) => height - pad.bottom - ((y - y0) / (y1 - y0)) * (height - pad.top - pad.bottom);
  const right = width - pad.right;
  const bottom = height - pad.bottom;

  const narrow = width < 520;
  let grid = "";
  for (let tick = Math.ceil(x0); tick <= Math.floor(x1); tick++) {
    grid += `<line class="grid" x1="${px(tick)}" x2="${px(tick)}" y1="${pad.top}" y2="${bottom}"/>`;
    // On a phone there is only room to label every other power of two.
    if (narrow && tick % 2) continue;
    grid += `<text class="tick" x="${px(tick)}" y="${bottom + 17}" text-anchor="middle">${powerLabel(tick)}</text>`;
  }
  for (let tick = Math.ceil(y0); tick <= Math.floor(y1); tick++) {
    grid +=
      `<line class="grid" x1="${pad.left}" x2="${right}" y1="${py(tick)}" y2="${py(tick)}"/>` +
      `<text class="tick" x="${pad.left - 9}" y="${py(tick) + 4}" text-anchor="end">${powerLabel(tick)}</text>`;
  }

  // Equal-ADP diagonals: log2(cells) + log2(cycles) = -log2(gain).
  let iso = "";
  for (let gain = 0; gain <= 5; gain++) {
    const yLeft = -gain - x0;
    const yRight = -gain - x1;
    if (yLeft < y0 || yRight > y1) continue;
    iso += `<line class="iso ${gain ? "" : "unity"}" x1="${px(x0)}" y1="${py(yLeft)}" x2="${px(x1)}" y2="${py(yRight)}"/>`;
    // Label where the line leaves the plot: the right edge if it gets there, else the bottom.
    const exitsRight = yRight >= y0;
    const lx = exitsRight ? right - 6 : px(-gain - y0) - 8;
    const ly = exitsRight ? py(yRight) - 7 : bottom - 7;
    if (gain && lx > pad.left + (narrow ? 90 : 230) && ly > pad.top + 12) {
      iso += `<text class="iso-label" x="${lx}" y="${ly}" text-anchor="end">${2 ** gain}×</text>`;
    }
  }
  const worse = `M${px(x0)},${py(-x0)}L${px(x1)},${py(-x1)}L${right},${pad.top}L${pad.left},${pad.top}z`;

  const marks = planePoints
    .map(
      (point, index) =>
        `<path class="mark" data-i="${index}" style="--vendor:${maker(point.model)[1]}" transform="translate(${px(point.x).toFixed(1)},${py(point.y).toFixed(1)})" d="${MARKERS[point.shape % MARKERS.length](5)}"/>`,
    )
    .join("");

  host.innerHTML =
    `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" ` +
    `aria-label="Scatter plot of ${planePoints.length} correct designs: cycles and cells relative to each problem's baseline, on logarithmic axes. The operator table below lists the same runs.">` +
    `<defs><clipPath id="plane-clip"><rect x="${pad.left}" y="${pad.top}" width="${right - pad.left}" height="${bottom - pad.top}"/></clipPath></defs>` +
    `<g clip-path="url(#plane-clip)"><path class="worse" d="${worse}"/></g>` +
    grid +
    `<g clip-path="url(#plane-clip)">${iso}</g>` +
    `<text class="region-label" x="${right - 12}" y="${pad.top + 22}" text-anchor="end">worse than the baseline</text>` +
    `<text class="iso-label" x="${pad.left + 10}" y="${bottom - 7}">${narrow ? "better by" : "better than the baseline by"}</text>` +
    `<rect class="frame" x="${pad.left}" y="${pad.top}" width="${right - pad.left}" height="${bottom - pad.top}"/>` +
    `<text class="axis-title" x="${(pad.left + right) / 2}" y="${height - 6}" text-anchor="middle">cycles, relative to baseline</text>` +
    `<text class="axis-title" transform="translate(12,${(pad.top + bottom) / 2}) rotate(-90)" text-anchor="middle">cells, relative to baseline</text>` +
    `<g class="marks">${marks}</g>` +
    `<circle class="origin" cx="${px(0)}" cy="${py(0)}" r="6.5"/>` +
    `<text class="origin-label" x="${px(0) - 11}" y="${py(0) + 17}" text-anchor="end">baseline</text>` +
    `</svg><div class="plane-tip" hidden></div>`;

  const shown = [...new Set(planePoints.map((point) => point.shape))];
  $("#plane-legend").innerHTML = shown
    .map((shape) => {
      const problem = data.problems[shape];
      return (
        `<li data-shape="${shape}"><svg viewBox="-8 -8 16 16" width="14" height="14" aria-hidden="true">` +
        `<path class="mark" d="${MARKERS[shape % MARKERS.length](5)}"/></svg>${escapeHtml(problem.title)}</li>`
      );
    })
    .join("");
}

/** Emphasize the marks that satisfy `matches`; null clears the emphasis. */
function focusPlane(matches) {
  const svg = $("#plane svg");
  if (!svg) return;
  svg.classList.toggle("focused", Boolean(matches));
  for (const mark of $$(".mark", svg)) {
    mark.classList.toggle("on", Boolean(matches) && matches(planePoints[Number(mark.dataset.i)]));
  }
}

function showPlaneTip(mark) {
  const host = $("#plane");
  const tip = $(".plane-tip", host);
  const point = planePoints[Number(mark.dataset.i)];
  const { run } = point;
  tip.innerHTML =
    `<strong>${escapeHtml(displayName(point.model))}</strong>` +
    `<span>${escapeHtml(point.problem.title)}</span>` +
    `<span>${formatInteger(run.cells)} cells × ${formatInteger(run.cycles)} cycles</span>` +
    `<b>${formatRatio(run.ratio)} ${run.ratio > 1 ? "better" : "of the baseline's score"}</b>`;
  tip.hidden = false;
  const box = mark.getBoundingClientRect();
  const frame = host.getBoundingClientRect();
  const x = box.left - frame.left + box.width / 2;
  const left = Math.min(Math.max(x - tip.offsetWidth / 2, 0), frame.width - tip.offsetWidth);
  const above = box.top - frame.top - tip.offsetHeight - 10;
  tip.style.left = `${left}px`;
  tip.style.top = `${above < 0 ? box.bottom - frame.top + 10 : above}px`;
}

function setupPlane() {
  const host = $("#plane");
  if (!host) return;
  host.addEventListener("pointerover", (event) => {
    const mark = event.target.closest(".mark");
    if (!mark) return;
    const point = planePoints[Number(mark.dataset.i)];
    focusPlane((other) => other.model === point.model);
    showPlaneTip(mark);
  });
  host.addEventListener("pointerout", (event) => {
    if (!event.target.closest(".mark")) return;
    focusPlane(null);
    $(".plane-tip", host).hidden = true;
  });
  host.addEventListener("click", (event) => {
    const mark = event.target.closest(".mark");
    if (!mark) return;
    const point = planePoints[Number(mark.dataset.i)];
    openRuns(point.model.label, point.problem.name, point.run.attempt);
  });

  // Pointing at an operator in the legend, or a model in the ranking, picks out its designs.
  const legend = $("#plane-legend");
  legend.addEventListener("pointerover", (event) => {
    const item = event.target.closest("[data-shape]");
    if (item) focusPlane((point) => point.shape === Number(item.dataset.shape));
  });
  legend.addEventListener("pointerleave", () => focusPlane(null));
  const rows = $("#leaderboard-rows");
  rows.addEventListener("pointerover", (event) => {
    const row = event.target.closest("[data-row-model]");
    if (row) focusPlane((point) => point.model.label === row.dataset.rowModel);
  });
  rows.addEventListener("pointerleave", () => focusPlane(null));

  let frame = 0;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(renderPlane);
  });
}

function renderMeta() {
  const meta = data.meta;
  fill("#meta-generated", meta.generated?.slice(0, 10) || "—");
  fill("#meta-commit", meta.git_commit?.slice(0, 10) || "—");
  const slots = data.models.reduce((total, model) => total + model.runs.length, 0);
  const problemCount = catalogProblems.length
    ? `${data.problems.length} of ${catalogProblems.length} problems`
    : `${data.problems.length} problems`;
  const published = meta.generated ? `, ${meta.generated.slice(0, 10)}` : "";
  fill("#page-summary", `${data.models.length} models, ${problemCount}, ${slots} runs${published}`);
  for (const link of $$("a[data-results-download]")) link.href = datasets[datasetKey];
  fill(
    "#pilot-note",
    `Round ${meta.pilot}, ${protocolName(meta.protocol)}. Up to ${meta.max_turns} agent turns per run, ` +
      `${budgetSummary(meta)}. Submissions are frozen, then scored on held-out cases.`,
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
      .join(", ");
    outcomes = `<p class="rc-meta">${formatInteger(model.scored)} scored, ${formatInteger(model.unscored)} unscored: ${counts}</p>`;
  }
  const reasoning = `<p class="rc-meta">${escapeHtml(model.label)}<br>Reasoning: ${escapeHtml(reasoningSummary(model).detail)}</p>`;

  dialog.innerHTML =
    `<div class="dialog-head">` +
    `<div><p class="dialog-kicker">Frozen run records, ${escapeHtml(data.meta.pilot)}</p><h2 id="dialog-title">${escapeHtml(displayName(model))}</h2></div>` +
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
    const timedOut = run.timed_out ? ", agent timed out" : "";
    timing = `${formatInteger(Math.round(run.duration_s))}s, ${formatInteger(checkCount)} agent check${plural}${timedOut}`;
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
    `<p class="rc-meta">Attempt ${run.attempt}, ${timing}</p>` +
    `<div class="rc-ratio">${run.correct ? formatRatio(run.ratio) : "—"}</div>` +
    `<p class="metric-caption">${run.correct ? "baseline ADP / design ADP" : "No valid ADP score"}</p>` +
    `<p class="rc-meta">${formatInteger(run.cells)} cells × ${formatInteger(run.cycles)} cycles<br>ADP ${formatInteger(run.adp)}<br>` +
    `Stage: ${escapeHtml(run.stage || "not reported")}, audit ${audit}</p>` +
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
    `<p class="evidence-title">${escapeHtml(protocolName(generation.protocol))}, ${escapeHtml(run.outcome)}</p>` +
    `<p class="rc-meta">${formatInteger(generation.turns)} / ${formatInteger(generation.max_turns)} model turns, ` +
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
    `<span class="problem-baseline">${formatInteger(baseline.cells)} cells × ${formatInteger(baseline.cycles)} cycles</span>` +
    `<span class="problem-chevron" aria-hidden="true">+</span>` +
    `</summary>` +
    `<div class="problem-content">` +
    `<p class="pc-desc">${problem.transactions} back-to-back transactions, ${problem.out_len} output word${words} each. ` +
    `Exact integer arithmetic, with no reset between transactions.</p>` +
    `<div class="chip-row">${params}</div>` +
    `<div class="pc-metrics">` +
    `<div><strong>${formatInteger(baseline.cells)}</strong><span>baseline cells</span></div>` +
    `<div><strong>${formatInteger(baseline.cycles)}</strong><span>baseline cycles</span></div>` +
    `<div><strong>${formatRatio(sanity.correct ? sanity.ratio : 0)}</strong><span>sanity ADP improvement</span></div>` +
    `</div>` +
    `<p class="bar-title">Area–delay product, lower is better</p>` +
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
    `<details><summary>Interface, arithmetic and edge cases</summary>` +
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
  setupPlane();
  window.addEventListener("resize", updateScrollHints);
}

/** Buttons with data-<name>="value": clicking one selects it and re-renders. */
function bindSegmentedControl(name, select) {
  const buttons = $$(`[data-${name}]`);
  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-pressed") === "true") return;
      select(button.dataset[name]);
      for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
      renderResults(name === "score");
    });
  }
}

async function getJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`Could not load ${path} (${response.status})`);
  return response.json();
}

async function loadData() {
  const onProblemPage = Boolean($("#problem-grid"));
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
    onProblemPage ? null : getJSON(leaderboardPath.replace(/leaderboard\.json$/, "reasoning.json")).catch(() => null),
  ]);
  if (!Array.isArray(leaderboard.models) || !Array.isArray(problems?.problems || leaderboard.problems)) {
    throw new Error("Invalid published dataset");
  }

  catalogProblems = Array.isArray(problems?.problems) ? problems.problems : [];
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
    renderPlane();
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
