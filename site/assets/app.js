/* Published records are the source of truth. No framework or build step. */
import { findExecution } from "./evidence.mjs";
import { parseCatalog } from "./catalog.mjs";
import { budgetSummary, outputLimit } from "./budget.mjs";
import { agentState } from "./outcomes.mjs";

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
    label.split("/").pop().replace(/\s*\[.*\]$/, "").replace(/-free$/, "");
  let data, dialog, returnFocus;
  let datasets = {};
  let datasetKey = "";
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
  function state(run) {
    if (run.correct)
      return run.ratio > 1
        ? { cls: "beat", label: "Beat baseline", value: ratio(run.ratio) }
        : { cls: "correct", label: "Correct, ≤ 1×", value: ratio(run.ratio) };
    return agentState(run) || { cls: "fail", label: "Incorrect RTL", value: "wrong" };
  }
  function fill(selector, text) {
    $$(selector).forEach((el) => (el.textContent = text));
  }

  let scoreMetric = "beat_rate",
    operatorMetric = "ratio";
  const names = {
    "mimo-v2.5": "MiMo V2.5",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "qwen3.8-flash": "Qwen3.8 Flash",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "kimi-k2.6": "Kimi K2.6",
    "minimax-m2.7": "MiniMax M2.7",
  };
  const displayName = (model) => names[short(model.label)] || short(model.label);
  // Matched against the model id after the provider prefix, in order.
  const logos = [
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
  function modelIcon(model) {
    const id = short(model.label);
    const logo = logos.find(([pattern]) => pattern.test(id))?.[1];
    return logo
      ? `<img src="assets/logos/${logo}.svg" alt="" width="15" height="15">`
      : esc(displayName(model).slice(0, 1));
  }
  function requestedReasoning(settings) {
    const thinking = settings.thinking || {}, parts = [];
    if (thinking.type === "disabled" || settings.reasoning?.enabled === false) parts.push("off");
    else if (thinking.type === "enabled" || settings.reasoning?.enabled === true)
      parts.push(thinking.budget_tokens ? `on, ${int(thinking.budget_tokens)}-token budget` : "on");
    if (settings.reasoning_effort) parts.push(`effort ${settings.reasoning_effort}`);
    return parts.join(" + ") || "provider default";
  }
  function reasoningSummary(model) {
    const settings = model.runs.find((r) => r.generation?.generation_settings)?.generation.generation_settings;
    const measured = model.runs.map((r) => r.reasoning).filter(Boolean);
    const sum = (key) => measured.reduce((n, r) => n + (Number(r[key]) || 0), 0);
    const total = sum("reasoning_chars") + sum("answer_chars");
    const share = measured.length && total ? sum("reasoning_chars") / total : null;
    const asked = settings ? requestedReasoning(settings) : "";
    const text = share === null ? "—" : share > 0 ? pct(share) : "None";
    const detail = share === null
      ? "Reasoning was not measured for this model."
      : `${pct(share)} of generated characters were reasoning, on ${int(sum("turns_with_reasoning"))} of ${int(sum("turns"))} turns.`;
    return { text, asked, detail: asked ? `${detail} Requested: ${asked}.` : detail };
  }
  function modelButton(model) {
    return `<button class="model-button" data-model="${esc(model.label)}" title="${esc(model.label)}" aria-label="Inspect ${esc(displayName(model))} runs"><span class="model-icon" aria-hidden="true">${modelIcon(model)}</span><span class="model-label">${esc(displayName(model))}</span></button>`;
  }
  function renderResults() {
    if (!$("#leaderboard-rows") || !data) return;
    const models = ranked(data.models).sort(
      (a, b) => b[scoreMetric] - a[scoreMetric],
    );
    const best = Math.max(0, ...models.map((m) => m[scoreMetric]));
    const isRate = scoreMetric !== "geomean";
    const label = {
      beat_rate: "Correct & better than baseline",
      correctness_rate: "Passed all correctness checks",
      geomean: "Geometric mean ADP ratio · correct runs only",
    }[scoreMetric];
    fill("#rank-metric-label", {beat_rate: "Beat baseline", correctness_rate: "Correctness", geomean: "ADP gain"}[scoreMetric]);
    fill("#interval-label", isRate ? "" : "/ correct runs only");
    $("#chart-axis").innerHTML = [0, .25, .5, .75, 1].map((n) => `<span>${isRate ? pct(n) : `${(n * best).toFixed(1)}×`}</span>`).join("");
    fill(
      "#score-note",
      isRate
        ? "Rates use all scheduled slots. Unscored outcomes are not incorrect RTL; inspect each model."
        : "Geometric mean · correct runs only · higher is better.",
    );
    $("#leaderboard-rows").innerHTML =
      models
        .map((m) => {
          const value = m[scoreMetric], reasoning = reasoningSummary(m);
          const text = isRate ? pct(value) : ratio(value);
          const width = isRate
            ? value * 100
            : best > 0
              ? (Math.max(0, value) / best) * 100
              : 0;
          const tooltip = isRate
            ? `${label}: ${text}. ${m.correct}/${m.attempts} confirmed correct slots.${m.unscored ? ` ${m.unscored} unscored; inspect typed outcomes.` : ""}`
            : `${text} geometric mean across ${m.correct} correct attempts; ${m.attempts} total attempts.`;
          return `<tr><th scope="row">${modelButton(m)}</th><td><button class="comparison-bar ${value === best && value > 0 ? "best" : ""}" data-model="${esc(m.label)}" title="${esc(tooltip)}" aria-label="${esc(displayName(m))}: ${esc(tooltip)}"><span class="comparison-track" aria-hidden="true"><span class="comparison-fill" style="width:${width}%"></span></span></button></td><td class="primary-stat"><strong>${text}</strong></td><td class="numeric-stat">${m.correct}/${m.attempts}</td><td class="numeric-stat">${ratio(m.geomean)}</td><td class="numeric-stat reasoning-stat" title="${esc(reasoning.detail)}"><strong>${reasoning.text}</strong>${reasoning.asked ? `<span>asked: ${esc(reasoning.asked)}</span>` : ""}</td><td class="numeric-stat">${m.attempts}</td></tr>`;
        })
        .join("");
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
          .join("")
      }</tbody></table>`;
    fill(
      "#matrix-note",
      operatorMetric === "ratio"
        ? "ADP ratio = baseline / design · higher is better."
        : `${operatorMetric === "cells" ? "Cell count" : "Cycle count"} · lower is better.`,
    );
  }
  function runEvidence(run) {
    const execution = findExecution(run);
    if (!execution) return '<p class="rc-meta">Original Actions evidence unavailable.</p>';
    const g = run.generation || {}, usage = g.usage || {};
    const cap = outputLimit(data.meta, g.model);
    return `<section class="run-evidence" aria-label="Original GitHub agent run"><p class="evidence-title">Agent-assisted v1 · ${esc(run.outcome)}</p><p class="rc-meta">${int(g.turns)} / ${int(g.max_turns)} model turns · ${int(g.dev_checks)} development checks. ${g.outcome === "submitted" ? "The explicit final submission was frozen for held-out scoring; inspect the outcome above to see whether scoring completed." : "No finalized submission was received; no held-out correctness score is claimed."} Development feedback is not a benchmark pass.</p><div class="evidence-links"><a href="${esc(execution.job)}" target="_blank" rel="noopener">Scoring job &amp; logs ↗</a><a href="${esc(execution.workflow)}" target="_blank" rel="noopener">Generation jobs &amp; artifacts ↗</a><a href="${esc(execution.code)}" target="_blank" rel="noopener">Evaluated code ↗</a></div><p class="rc-meta">${g.incomplete_usage ? "At least " : ""}${int(usage.output_tokens ?? usage.completion_tokens)} reported output tokens across turns. Requested limit: ${cap ? int(cap) : "unavailable"} tokens per response. Execution health: ${esc(run.execution_health)}.</p><details><summary>Generation settings</summary><p class="hash-value">${esc(JSON.stringify(g.generation_settings || {}))}</p></details><p class="evidence-retention">Transcripts remain private Actions artifacts.</p></section>`;
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
        const unknownScore = r.correct === null;
        const checkCount = r.generation?.dev_checks;
        const timing = unknownScore ? "scoring details unavailable" : `${int(Math.round(r.duration_s))}s · ${int(checkCount)} agent check${checkCount === 1 ? "" : "s"}${r.timed_out ? " · agent timed out" : ""}`;
        return `<article class="run-card"><div class="rc-head"><strong>${esc(title(r.problem))}</strong><span class="rc-status ${s.cls}">${s.label}</span></div><p class="rc-meta">Attempt ${r.attempt} · ${timing}</p><div class="rc-ratio">${r.correct ? ratio(r.ratio) : "—"}</div><p class="metric-caption">${r.correct ? "baseline ADP / design ADP" : "No valid ADP score"}</p><p class="rc-meta">${int(r.cells)} cells × ${int(r.cycles)} cycles<br>ADP ${int(r.adp)}<br>Stage: ${esc(r.stage || "not reported")} · audit ${unknownScore ? "unknown" : r.audit_ok ? "passed" : "not passed"}</p>${[
          r.correctness,
          r.error,
        ]
          .filter(Boolean)
          .map((text) => `<p class="rc-detail">${esc(text)}</p>`)
          .join(
            "",
          )}${runEvidence(r)}<details><summary>Inspect artifact hashes</summary><p>Submission SHA-256</p><p class="hash-value">${esc(r.submission_sha256 || "Not recorded")}</p><p>Netlist SHA-256</p><p class="hash-value">${esc(r.netlist_sha256 || "Not recorded")}</p></details></article>`;
      })
      .join(
        "",
      )}</div><p class="pilot-note">Ratios are area–delay improvements, not clock-speed measurements. <a href="${datasets[datasetKey]}">Read the source records ↗</a></p></div>`;
    $(".dialog-summary", dialog).insertAdjacentHTML(
      "afterend",
      `<p class="rc-meta">${esc(model.label)}<br>Reasoning: ${esc(reasoningSummary(model).detail)}</p>`,
    );
    if (model.outcomes)
      $(".dialog-summary", dialog).insertAdjacentHTML("afterend", `<p class="rc-meta">${int(model.scored)} scored · ${int(model.unscored)} unscored. ${Object.entries(model.outcomes).map(([kind, count]) => `${esc(kind.replaceAll("_", " "))}: ${int(count)}`).join(" · ")}</p>`);
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
        return `<details class="problem-card problem-accordion" name="problems" id="${esc(p.name)}"><summary class="problem-toggle"><span class="problem-number">${String(i + 1).padStart(2, "0")}</span><span class="problem-title">${esc(p.title)}</span><span class="problem-chevron" aria-hidden="true">+</span></summary><div class="problem-content"><p class="pc-desc">${p.transactions} back-to-back transactions · ${p.out_len} output word${p.out_len === 1 ? "" : "s"} per transaction. Exact integer arithmetic, with no reset between transactions.</p><div class="chip-row">${Object.entries(
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
    const m = data.meta;
    fill("#meta-generated", m.generated?.slice(0, 10) || "—");
    fill("#meta-commit", m.git_commit?.slice(0, 10) || "—");
    fill("#attempt-heading", "Slots");
    $$("a[data-results-download]").forEach((a) => (a.href = datasets[datasetKey]));
    fill("#pilot-note", `Agent-assisted v1 · shared read/write/check/submit operations · up to ${m.max_turns} model turns per slot · ${budgetSummary(m)} · development checks only during generation, then frozen held-out scoring. Incorrect RTL remains a failed measurement; provider, submission and interrupted outcomes are shown separately. Rates use all scheduled slots, not only completed scores. GitHub job success means evidence was recorded, not that the model passed.`);
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
      // The leaderboard is the newest agent-assisted-v1 batch; the publisher prepends new batches.
      const catalog = parseCatalog(await getJSON("data/evaluations.json"));
      datasets = catalog.paths;
      datasetKey = (catalog.entries.find((e) => e.protocol === "agent-assisted-v1") || {}).id || catalog.defaultId;
      const [leaderboard, problems, reasoningFile] = await Promise.all([
        getJSON(datasets[datasetKey]),
        getJSON("data/problems.json").catch(() => null),
        // Backfilled counts for batches published before receipts recorded them.
        getJSON(datasets[datasetKey].replace(/leaderboard\.json$/, "reasoning.json")).catch(() => null),
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
      const backfill = reasoningFile?.schema_version === 1 ? reasoningFile.runs || {} : {};
      for (const model of data.models)
        for (const run of model.runs)
          run.reasoning = run.generation?.reasoning_measured || backfill[`${short(model.label)}/${run.problem}`] || null;
      renderResults();
      renderProblems();
      renderMeta();
    } catch (error) {
      $("#main").insertAdjacentHTML(
        "afterbegin",
        '<div class="error-state" role="alert"><strong>Published results could not load.</strong><p>Refresh to retry.</p></div>',
      );
      $$(".loading").forEach((el) => el.remove());
      console.error(error);
    }
  });
})();
