/* Published records are the source of truth. No framework or build step. */
import { findReplay, findExecution } from "./evidence.mjs";
import { parseCatalog } from "./catalog.mjs";

(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const int = (n) =>
    Number.isFinite(n) && n >= 0 ? n.toLocaleString("en-US") : "—";
  const pct = (n) => (Number.isFinite(n) ? `${Math.round(n * 100)}%` : "—");
  const ratio = (n) => (n > 0 ? `${n.toFixed(2)}×` : "—");
  const short = (label) =>
    label.replace(/^opencode\//, "").replace(/-free$/, "");
  let data, dialog, returnFocus, evidence;
  let datasets = {
    "pilot-001": "data/leaderboard.json",
    "go-core-20260922": "data/go-core-20260922/leaderboard.json",
  };
  let datasetKey = "pilot-001";
  const title = (name) =>
    data.problems.find((p) => p.name === name)?.title || name;
  const ranked = (models) =>
    [...models].sort(
      (a, b) =>
        b.beat_rate - a.beat_rate ||
        b.correctness_rate - a.correctness_rate ||
        b.geomean - a.geomean ||
        a.label.localeCompare(b.label),
    );
  // Match report.RunSummary.kind, including a timeout with a scored wrong design.
  function state(run) {
    if (run.record_origin === "github-job-status-only")
      return { cls: "infra", label: "Job failed · score unavailable", value: "unknown" };
    if (run.record_origin === "github-generation-only")
      return { cls: "infra", label: "Scoring interrupted · score unknown", value: "unknown" };
    if (run.correct)
      return run.ratio > 1
        ? { cls: "beat", label: "Beat baseline", value: ratio(run.ratio) }
        : { cls: "correct", label: "Correct, ≤ 1×", value: ratio(run.ratio) };
    if (run.generation?.error?.startsWith("not requested"))
      return { cls: "infra", label: "Not requested (safety stop)", value: "skipped" };
    if (["length", "max_tokens"].includes(run.generation?.finish_reason))
      return { cls: "infra", label: "Generation reached output cap", value: "cap" };
    if (run.generation?.error || run.generation?.invalid_rtl)
      return { cls: "infra", label: "Generation failed", value: "no RTL" };
    if (
      run.error ||
      ["no result", "no_result"].includes(run.stage) ||
      (run.timed_out && !run.stage)
    )
      return { cls: "infra", label: "Infrastructure", value: "infra" };
    return { cls: "fail", label: "Incorrect RTL", value: "wrong" };
  }
  function wilson(k, n) {
    if (!n) return [0, 0];
    const z2 = 1.96 ** 2,
      p = k / n,
      d = 1 + z2 / n,
      c = (p + z2 / (2 * n)) / d,
      h = (1.96 * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / d;
    return [Math.max(0, c - h), Math.min(1, c + h)];
  }
  function fill(selector, text) {
    $$(selector).forEach((el) => (el.textContent = text));
  }

  let scoreMetric = "beat_rate",
    operatorMetric = "ratio";
  const names = {
    "opencode/mimo-v2.5-free": "MiMo v2.5",
    "opencode/muse-spark-1.3-contributor-free": "Muse Spark 1.3",
    "opencode/nemotron-3-ultra-free": "Nemotron 3 Ultra",
    "opencode/nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning",
    "opencode/ling-3.0-flash-fin-free": "Ling 3.0 Flash Fin",
    "opencode-go/mimo-v2.5 [single-shot]": "MiMo V2.5",
    "opencode-go/deepseek-v4.1-flash [single-shot]": "DeepSeek V4.1 Flash",
    "opencode-go/qwen3.8-flash [single-shot]": "Qwen3.8 Flash",
    "opencode-go/glm-5.3-flash [single-shot]": "GLM-5.3-Flash",
    "opencode-go/kimi-k2.6 [single-shot]": "Kimi K2.6",
    "opencode-go/minimax-m2.7 [single-shot]": "MiniMax M2.7",
  };
  const displayName = (model) => names[model.label] || short(model.label);
  function modelButton(model) {
    return `<button class="model-button" data-model="${esc(model.label)}" title="${esc(model.label)}" aria-label="Inspect ${esc(displayName(model))} runs"><span class="model-icon" aria-hidden="true">${esc(displayName(model).slice(0, 1))}</span><span class="model-label">${esc(displayName(model))}</span></button>`;
  }
  function renderResults() {
    if (!$("#leaderboard-rows") || !data) return;
    const query = $("#model-search").value.trim().toLowerCase();
    const all = ranked(data.models).sort(
      (a, b) => b[scoreMetric] - a[scoreMetric],
    );
    const models = all.filter((m) =>
      `${m.label} ${displayName(m)}`.toLowerCase().includes(query),
    );
    const best = Math.max(0, ...all.map((m) => m[scoreMetric]));
    const isRate = scoreMetric !== "geomean";
    const label = {
      beat_rate: "Correct & better than baseline",
      correctness_rate: "Passed all correctness checks",
      geomean: "Geometric mean ADP ratio · correct runs only",
    }[scoreMetric];
    fill("#rank-metric-label", {beat_rate: "Beat baseline", correctness_rate: "Correctness", geomean: "ADP gain"}[scoreMetric]);
    fill("#interval-label", isRate ? "/ 95% confidence interval" : "/ correct runs only");
    $("#chart-axis").innerHTML = [0, .25, .5, .75, 1].map((n) => `<span>${isRate ? pct(n) : `${(n * best).toFixed(1)}×`}</span>`).join("");
    fill(
      "#score-note",
      isRate
        ? "White lines: 95% confidence intervals."
        : "Geometric mean · correct runs only · higher is better.",
    );
    fill(
      "#leaderboard-count",
      `${models.length} / ${data.models.length} models`,
    );
    $("#leaderboard-rows").innerHTML =
      models
        .map((m) => {
          const value = m[scoreMetric],
            [low, high] = wilson(
              scoreMetric === "beat_rate" ? m.beating : m.correct,
              m.attempts,
            );
          const text = isRate ? pct(value) : ratio(value);
          const width = isRate
            ? value * 100
            : best > 0
              ? (Math.max(0, value) / best) * 100
              : 0;
          const tooltip = isRate
            ? `${label}: ${text}; 95% Wilson interval ${pct(low)}–${pct(high)}. ${m.correct}/${m.attempts} correct attempts.`
            : `${text} geometric mean across ${m.correct} correct attempts; ${m.attempts} total attempts.`;
          return `<tr><th scope="row">${modelButton(m)}</th><td><button class="comparison-bar ${value === best && value > 0 ? "best" : ""}" data-model="${esc(m.label)}" title="${esc(tooltip)}" aria-label="${esc(displayName(m))}: ${esc(tooltip)}"><span class="comparison-track" aria-hidden="true"><span class="comparison-fill" style="width:${width}%"></span>${isRate ? `<span class="confidence-whisker" style="left:${low * 100}%;width:${(high - low) * 100}%"></span>` : ""}</span></button></td><td class="primary-stat"><strong>${text}</strong>${isRate ? `<span class="interval-range" title="95% Wilson confidence interval">${pct(low)}–${pct(high)}</span>` : ""}</td><td class="numeric-stat">${m.correct}/${m.attempts}</td><td class="numeric-stat">${ratio(m.geomean)}</td><td class="numeric-stat">${m.attempts}</td></tr>`;
        })
        .join("") ||
      '<tr><td colspan="6" class="empty-state">No matching models. Try another name.</td></tr>';
    const bestByProblem = new Map(
      data.problems.map((p) => {
        const values = data.models
          .flatMap((m) => m.runs)
          .filter(
            (r) => r.problem === p.name && r.correct && r[operatorMetric] > 0,
          )
          .map((r) => r[operatorMetric]);
        return [
          p.name,
          values.length
            ? operatorMetric === "ratio"
              ? Math.max(...values)
              : Math.min(...values)
            : 0,
        ];
      }),
    );
    $("#heatmap").innerHTML =
      `<table class="results-table matrix-table"><thead><tr><th scope="col">Model</th>${data.problems.map((p) => `<th scope="col"><a href="problems.html#${esc(p.name)}">${esc(p.title)}</a></th>`).join("")}<th scope="col" class="correct-heading">Correct</th></tr></thead><tbody>${
        models
          .map(
            (m) =>
              `<tr><td>${modelButton(m)}</td>${data.problems
                .map((p) => {
                  const runs = m.runs
                    .filter((r) => r.problem === p.name)
                    .sort((a, b) => a.attempt - b.attempt);
                  return `<td>${
                    runs.length
                      ? runs
                          .map((r) => {
                            const s = state(r),
                              value = r[operatorMetric],
                              best = bestByProblem.get(p.name);
                            const valid = r.correct && value > 0,
                              width =
                                valid && best > 0
                                  ? (operatorMetric === "ratio"
                                      ? value / best
                                      : best / value) * 100
                                  : 0;
                            const text = valid
                              ? operatorMetric === "ratio"
                                ? ratio(value)
                                : int(value)
                              : r.correct
                                ? "—"
                                : s.value;
                            const tooltip = `${displayName(m)} · ${p.title} · attempt ${r.attempt}: ${s.label}. ${valid ? text + " " + operatorMetric : "No valid score"}`;
                            return `<button class="result-bar ${s.cls} ${valid && value === best ? "best" : ""}" style="--fill:${width}%" data-model="${esc(m.label)}" data-problem="${esc(p.name)}" data-attempt="${r.attempt}" title="${esc(tooltip)}" aria-label="${esc(tooltip)}">${text}${runs.length > 1 ? ` <small> · #${r.attempt}</small>` : ""}${valid && value === best ? '<span class="star" aria-hidden="true">★</span>' : ""}</button>`;
                          })
                          .join("")
                      : '<span class="result-bar none" aria-label="Not run">—</span>'
                  }</td>`;
                })
                .join(
                  "",
                )}<td><span class="correct-count" title="${m.correct}/${m.attempts} correct"><span>${m.correct}/${m.attempts}</span></span></td></tr>`,
          )
          .join("") ||
        `<tr><td colspan="${data.problems.length + 2}" class="empty-state">No matching models.</td></tr>`
      }</tbody></table>`;
    fill(
      "#matrix-note",
      operatorMetric === "ratio"
        ? "ADP ratio = baseline / design · higher is better."
        : `${operatorMetric === "cells" ? "Cell count" : "Cycle count"} · lower is better.`,
    );
  }
  function replayEvidence(run) {
    const execution = findExecution(run);
    if (execution) {
      const g = run.generation || {}, usage = g.usage || {};
      if (g.evidence_unavailable)
        return `<section class="run-evidence" aria-label="GitHub job status only"><p class="evidence-title">Job ${esc(execution.conclusion)} · final artifacts unavailable</p><p class="rc-meta">This entry records the observed Actions job status only. Per-problem generation, scores, RTL and token usage are unknown; no score or replay verification is claimed.</p><div class="evidence-links"><a href="${esc(execution.job)}" target="_blank" rel="noopener">Failed job &amp; logs ↗</a><a href="${esc(execution.workflow)}" target="_blank" rel="noopener">Workflow ↗</a></div></section>`;
      const interrupted = run.record_origin === "github-generation-only";
      return `<section class="run-evidence" aria-label="Original GitHub model run"><p class="evidence-title">${interrupted ? "Generated in GitHub Actions · scoring interrupted" : "Original GitHub Actions model attempt"}</p><p class="rc-meta">New single-shot attempt, not a replay. Job: ${esc(execution.conclusion)}. ${interrupted ? "Generation and saved RTL are available; no completed score is claimed." : "Correctness is the measured outcome above, when scoring completed."}</p><div class="evidence-links"><a href="${esc(execution.job)}" target="_blank" rel="noopener">Model job &amp; logs ↗</a><a href="${esc(execution.workflow)}" target="_blank" rel="noopener">Workflow &amp; artifacts ↗</a><a href="${esc(execution.code)}" target="_blank" rel="noopener">Evaluated code ↗</a></div><p class="rc-meta">Output: ${int(usage.completion_tokens ?? usage.output_tokens)} tokens · finish: ${esc(g.finish_reason || "no completion")}<br>Reasoning response: ${int(g.response_diagnostics?.reasoning_chars)} characters</p><details><summary>Generation settings</summary><p class="hash-value">${esc(JSON.stringify(g.generation_settings || {}))}</p></details><p class="evidence-retention">Logs and raw response artifacts are retained for 90 days. Frozen records remain in the repository.</p></section>`;
    }
    if (run.generation)
      return '<p class="rc-meta">Original Actions evidence unavailable. This is not verified replay evidence.</p>';
    if (!evidence)
      return '<p class="rc-meta">Replay evidence unavailable. Scores are unaffected.</p>';
    const replay = findReplay(evidence, data.meta.pilot, run);
    if (!replay)
      return '<p class="rc-meta">No verified GitHub replay linked to this submission.</p>';
    return `<section class="run-evidence" aria-label="GitHub replay evidence"><p class="evidence-title">Replay verified <span>· ${esc(replay.date)}</span></p><p class="rc-meta">The frozen submission reproduced its correctness, cells, cycles and ratio. This is not a new model attempt.</p><div class="evidence-links"><a href="${esc(replay.job)}" target="_blank" rel="noopener">Replay job ↗</a><a href="${esc(replay.source)}" target="_blank" rel="noopener">Frozen Verilog ↗</a><a href="${esc(replay.workflow)}" target="_blank" rel="noopener">Workflow &amp; artifacts ↗</a></div><p class="evidence-retention">GitHub logs and artifacts may expire; the source is commit-pinned.</p></section>`;
  }
  function openRuns(label, problem, attempt) {
    const model = data.models.find((m) => m.label === label);
    if (!model) return;
    returnFocus = document.activeElement;
    const runs = model.runs.filter(
      (r) =>
        (!problem || r.problem === problem) &&
        (!attempt || r.attempt === attempt),
    );
    dialog.innerHTML = `<div class="dialog-head"><div><p class="eyebrow">FROZEN RUN RECORDS · ${esc(data.meta.pilot)}</p><h2 id="dialog-title">${esc(displayName(model))}</h2></div><button class="dialog-close" aria-label="Close run details" autofocus>×</button></div><div class="dialog-body"><div class="dialog-summary"><span class="badge">${model.beating}/${model.attempts} beat baseline</span><span class="badge">${model.correct}/${model.attempts} correct</span><span class="badge">${model.wrong_rtl} wrong RTL</span><span class="badge">${model.infra} infrastructure</span></div><div class="run-grid">${runs
      .map((r) => {
        const s = state(r);
        const unknownScore = ["github-job-status-only", "github-generation-only"].includes(r.record_origin);
        const timing = unknownScore ? "scoring details unavailable" : `${int(Math.round(r.duration_s))}s · ${int(r.history)} agent check${r.history === 1 ? "" : "s"}${r.timed_out ? " · agent timed out" : ""}`;
        return `<article class="run-card"><div class="rc-head"><strong>${esc(title(r.problem))}</strong><span class="rc-status ${s.cls}">${s.label}</span></div><p class="rc-meta">Attempt ${r.attempt} · ${timing}</p><div class="rc-ratio">${r.correct ? ratio(r.ratio) : "—"}</div><p class="metric-caption">${r.correct ? "baseline ADP / design ADP" : "No valid ADP score"}</p><p class="rc-meta">${int(r.cells)} cells × ${int(r.cycles)} cycles<br>ADP ${int(r.adp)}<br>Stage: ${esc(r.stage || "not reported")} · audit ${unknownScore ? "unknown" : r.audit_ok ? "passed" : "not passed"}</p>${[
          r.correctness,
          r.error,
        ]
          .filter(Boolean)
          .map((text) => `<p class="rc-detail">${esc(text)}</p>`)
          .join(
            "",
          )}${replayEvidence(r)}<details><summary>Inspect artifact hashes</summary><p>Submission SHA-256</p><p class="hash-value">${esc(r.submission_sha256 || "Not recorded")}</p><p>Netlist SHA-256</p><p class="hash-value">${esc(r.netlist_sha256 || "Not recorded")}</p></details></article>`;
      })
      .join(
        "",
      )}</div><p class="pilot-note">Ratios are area–delay improvements, not clock-speed measurements. <a href="${datasets[datasetKey]}">Read the source records ↗</a></p></div>`;
    const interval = (count) =>
      wilson(count, model.attempts).map(pct).join("–");
    $(".dialog-summary", dialog).insertAdjacentHTML(
      "afterend",
      `<p class="rc-meta">${esc(model.label)}<br>95% Wilson intervals · beat baseline ${interval(model.beating)} · correctness ${interval(model.correct)}</p>`,
    );
    $(".dialog-close", dialog).addEventListener("click", () => dialog.close());
    dialog.showModal();
    document.body.style.overflow = "hidden";
  }
  function renderProblems() {
    if (!$("#problem-grid")) return;
    $("#problem-grid").innerHTML = data.problems
      .map((p, i) => {
        const b = p.baseline || {},
          s = p.sanity || {},
          max = Math.max(b.adp || 0, s.adp || 0),
          src = `https://github.com/arygupt/ADPBench/blob/main/problems/level${p.level}/${encodeURIComponent(p.name)}`;
        return `<details class="problem-card problem-accordion" name="problems" id="${esc(p.name)}"><summary class="problem-toggle"><span class="problem-number">${String(i + 1).padStart(2, "0")}</span><span class="problem-title">${esc(p.title)}</span><span class="badge">LEVEL ${p.level}</span><span class="problem-chevron" aria-hidden="true">+</span></summary><div class="problem-content"><p class="pc-desc">${p.transactions} back-to-back transactions · ${p.out_len} output word${p.out_len === 1 ? "" : "s"} per transaction. Exact integer arithmetic, with no reset between transactions.</p><div class="chip-row">${Object.entries(
          p.params || {},
        )
          .map(([k, v]) => `<span class="chip">${esc(k)} ${esc(v)}</span>`)
          .join(
            "",
          )}</div><div class="pc-metrics"><div><strong>${int(b.cells)}</strong><span>baseline cells</span></div><div><strong>${int(b.cycles)}</strong><span>baseline cycles</span></div><div><strong>${ratio(s.correct ? s.ratio : 0)}</strong><span>sanity ADP improvement</span></div></div><p class="eyebrow">AREA–DELAY PRODUCT · LOWER IS BETTER</p><div class="bar-pair">${[
          ["Baseline", b.adp],
          ["Sanity", s.correct ? s.adp : null],
        ]
          .map(
            ([label, value]) =>
              `<div class="bar-row"><span>${label}</span><span class="bar-track"><span class="bar-fill" style="width:${max > 0 && value > 0 ? (value / max) * 100 : 0}%;${label === "Sanity" ? "background:var(--muted)" : ""}"></span></span><span class="bar-num">${int(value)}</span></div>`,
          )
          .join(
            "",
          )}</div><details><summary>Interface, arithmetic & edge cases</summary><table class="contract-table"><tbody>${Object.entries(
          p.input_lens || {},
        )
          .map(
            ([k, v]) =>
              `<tr><th scope="row">${esc(k)}</th><td>${v} elements / transaction</td></tr>`,
          )
          .join("")}${Object.entries(p.quant || {})
          .map(
            ([k, v]) =>
              `<tr><th scope="row">${esc(k)}</th><td>${esc(v)}</td></tr>`,
          )
          .join(
            "",
          )}</tbody></table><div class="chip-row">${(p.directed || []).map((d) => `<span class="chip">${esc(d)}</span>`).join("")}</div></details><div class="pc-links"><a href="${src}/dut.py" target="_blank" rel="noopener">View executable spec ↗</a><a href="${src}/baseline.v" target="_blank" rel="noopener">Baseline RTL ↗</a></div></div></details>`;
      })
      .join("");
    const revealProblem = () => {
      let id;
      try { id = decodeURIComponent(location.hash.slice(1)); }
      catch { return; }
      const target = document.getElementById(id);
      if (target?.classList.contains("problem-accordion")) {
        target.open = true;
        target.scrollIntoView();
      }
    };
    revealProblem();
    window.addEventListener("hashchange", revealProblem);
  }

  function renderMeta() {
    const m = data.meta,
      runs = data.models.flatMap((m) => m.runs);
    fill("#meta-generated", m.generated?.slice(0, 10) || "—");
    fill("#meta-commit", m.git_commit?.slice(0, 10) || "—");
    fill(
      "#dataset-summary",
      `${m.pilot || "published pilot"} · ${data.models.length} models · ${data.problems.length} operators · ${runs.length} attempts`,
    );
    fill(
      "#replay-summary",
      evidence
        ? `${runs.filter((r) => findReplay(evidence, m.pilot, r)).length} submissions have verified GitHub replays. Select a model or result to inspect the evidence. Replays do not add attempts.`
        : "Replay evidence unavailable. Select a model or result to inspect its recorded score.",
    );
    fill(
      "#pilot-note",
      `${m.repetitions || 1} attempt(s) per model–problem pair · ${m.budget_s ? m.budget_s / 60 + " min budget" : "budget unreported"} · ${m.sandbox?.mode || "sandbox unreported"}. Cells × cycles is an area–delay proxy, not a power or physical-timing measurement. Small pilot; model variance is not yet established.`,
    );
    $$('a[data-results-download]').forEach((a) => a.href = datasets[datasetKey]);
    $$('a[data-report-link]').forEach((a) => a.href = datasets[datasetKey].replace('leaderboard.json', 'report.json'));
    if (m.protocol === "single-shot") {
      const execution = runs.map(findExecution).find(Boolean);
      fill("#dataset-summary", `${m.pilot} · ${data.models.length} models · ${data.problems.length} operators · ${runs.length} scheduled result slots`);
      fill("#attempt-heading", "Slots");
      if ($("#replay-summary")) $("#replay-summary").innerHTML = `${int(m.generation_requests)} saved model responses · ${m.incomplete_usage ? "at least " : ""}${int(m.output_tokens)} reported output tokens · ${runs.filter(r => r.correct).length}/${runs.length} confirmed correct. ${m.incomplete_evidence ? "Some scoring evidence is unavailable; unknown scores are not passes. " : ""}${execution ? `<a href="${execution.workflow}" target="_blank" rel="noopener">Open all ${data.models.length} GitHub model jobs ↗</a>` : "Actions evidence unavailable."}`;
      fill("#pilot-note", `Single-shot Go screen · at most one request per model–problem pair · ${int(m.max_output_tokens)} output-token cap/request · no repairs or retries · offline Docker scoring. Rates use all scheduled slots, including rejected requests and safety-stop skips; inspect each result for its status. Reasoning settings differ by model and are shown with each result. This dataset is separate from iterative pilot-001; their rankings are not directly comparable. A completed workflow is not a correctness or replay claim.`);
    }
  }
  function setupShell() {
    dialog = document.createElement("dialog");
    dialog.setAttribute("aria-labelledby", "dialog-title");
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      document.body.style.overflow = "";
      returnFocus?.focus();
    });
    document.addEventListener("click", (e) => {
      const button = e.target.closest("[data-model]");
      if (button && data)
        openRuns(
          button.dataset.model,
          button.dataset.problem,
          Number(button.dataset.attempt) || undefined,
        );
    });
    $$("[data-score]").forEach((button) =>
      button.addEventListener("click", () => {
        scoreMetric = button.dataset.score;
        $$("[data-score]").forEach((b) =>
          b.setAttribute("aria-pressed", String(b === button)),
        );
        renderResults();
      }),
    );
    $$("[data-metric]").forEach((button) =>
      button.addEventListener("click", () => {
        operatorMetric = button.dataset.metric;
        $$("[data-metric]").forEach((b) =>
          b.setAttribute("aria-pressed", String(b === button)),
        );
        renderResults();
      }),
    );
    $("#model-search")?.addEventListener("input", renderResults);
  }
  async function getJSON(path) {
    const response = await fetch(path);
    if (!response.ok)
      throw new Error(`Could not load ${path} (${response.status})`);
    return response.json();
  }
  document.addEventListener("DOMContentLoaded", async () => {
    setupShell();
    try {
      const selector = $("#dataset-select");
      if (selector) {
        const catalog = parseCatalog(await getJSON("data/evaluations.json"));
        datasets = catalog.paths;
        selector.innerHTML = catalog.entries.map(e => `<option value="${esc(e.id)}">${esc(e.label)}</option>`).join("");
        const requested = new URLSearchParams(location.search).get("dataset");
        datasetKey = Object.hasOwn(datasets, requested) ? requested : catalog.defaultId;
        selector.value = datasetKey;
        selector.addEventListener("change", () => {
          const url = new URL(location.href);
          url.searchParams.set("dataset", selector.value);
          location.assign(url.href);
        });
      }
      const [leaderboard, problems, publishedEvidence] = await Promise.all([
        getJSON(datasets[datasetKey]),
        getJSON("data/problems.json").catch(() => null),
        getJSON("data/evidence.json").catch(() => null),
      ]);
      if (
        !Array.isArray(leaderboard.models) ||
        !Array.isArray(problems?.problems || leaderboard.problems)
      )
        throw new Error("Invalid published dataset");
      data = {
        ...leaderboard,
        meta: leaderboard.meta || {},
        problems: $("#problem-grid") ? (problems?.problems || leaderboard.problems) : leaderboard.problems,
      };
      evidence = publishedEvidence?.schema_version === 1 && Array.isArray(publishedEvidence.runs)
        ? publishedEvidence : null;
      renderResults();
      renderProblems();
      renderMeta();
    } catch (error) {
      $("#main").insertAdjacentHTML(
        "afterbegin",
        '<div class="error-state" role="alert"><strong>Published results could not load.</strong><p>Refresh to retry or <a href="data/report.json">open the report</a>.</p></div>',
      );
      $$(".loading").forEach((el) => el.remove());
      fill("#dataset-summary", "Results unavailable");
      console.error(error);
    }
  });
})();
