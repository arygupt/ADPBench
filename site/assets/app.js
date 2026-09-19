/* Published records are the source of truth. No framework or build step. */
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
  const symbols = ["a·b", "Ax", "AB", "f∗g"];
  let data, dialog, returnFocus;
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
    if (run.correct)
      return run.ratio > 1
        ? { cls: "beat", label: "Beat baseline", value: ratio(run.ratio) }
        : { cls: "correct", label: "Correct, ≤ 1×", value: ratio(run.ratio) };
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
    fill("#score-heading", label);
    fill(
      "#score-note",
      isRate
        ? "Share of all recorded attempts · hover or inspect for 95% Wilson intervals."
        : "Correct-run geometric mean · bar = share of best · failures excluded from this secondary metric.",
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
          return `<tr><td>${modelButton(m)}</td><td><button class="result-bar ${value === best && value > 0 ? "best" : ""}" style="--fill:${width}%" data-model="${esc(m.label)}" title="${esc(tooltip)}" aria-label="${esc(displayName(m))}: ${esc(tooltip)}">${text}${value === best && value > 0 ? '<span class="star" aria-hidden="true">★</span>' : ""}</button></td><td><span class="correct-count" aria-label="${m.correct} of ${m.attempts} attempts correct" title="${m.correct}/${m.attempts} correct">${m.attempts <= 12 ? Array.from({ length: m.attempts }, (_, i) => `<i class="${i < m.correct ? "passed" : ""}" aria-hidden="true"></i>`).join("") : `<span>${m.correct}/${m.attempts}</span>`}</span></td></tr>`;
        })
        .join("") ||
      '<tr><td colspan="3" class="empty-state">No matching models.</td></tr>';
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
        ? "ADP ratio = baseline / design · higher is better · bar = share of best correct result per operator."
        : `${operatorMetric === "cells" ? "Cell count" : "Cycle count"} · lower is better · bar = best / value among correct results per operator. Incorrect designs receive no bar.`,
    );
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
        return `<article class="run-card"><div class="rc-head"><strong>${esc(title(r.problem))}</strong><span class="rc-status ${s.cls}">${s.label}</span></div><p class="rc-meta">Attempt ${r.attempt} · ${int(Math.round(r.duration_s))}s · ${int(r.history)} check${r.history === 1 ? "" : "s"}${r.timed_out ? " · agent timed out" : ""}</p><div class="rc-ratio">${r.correct ? ratio(r.ratio) : "—"}</div><p class="metric-caption">${r.correct ? "baseline ADP / design ADP" : "No valid ADP score"}</p><p class="rc-meta">${int(r.cells)} cells × ${int(r.cycles)} cycles<br>ADP ${int(r.adp)}<br>Stage: ${esc(r.stage || "not reported")} · audit ${r.audit_ok ? "passed" : "not passed"}</p>${[
          r.correctness,
          r.error,
        ]
          .filter(Boolean)
          .map((text) => `<p class="rc-detail">${esc(text)}</p>`)
          .join(
            "",
          )}<details><summary>Inspect artifact hashes</summary><p>Submission SHA-256</p><p class="hash-value">${esc(r.submission_sha256 || "Not recorded")}</p><p>Netlist SHA-256</p><p class="hash-value">${esc(r.netlist_sha256 || "Not recorded")}</p></details></article>`;
      })
      .join(
        "",
      )}</div><p class="pilot-note">Ratios are area–delay improvements, not clock-speed measurements. <a href="data/leaderboard.json">Read the source records ↗</a></p></div>`;
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
        return `<article class="problem-card" id="${esc(p.name)}"><div class="pc-top"><span class="pc-symbol">${symbols[i] || "ƒ"}</span><span class="badge">LEVEL ${p.level} / ${String(i + 1).padStart(3, "0")}</span></div><h3>${esc(p.title)}</h3><p class="pc-desc">${p.transactions} back-to-back transactions · ${p.out_len} output word${p.out_len === 1 ? "" : "s"} per transaction. Exact integer arithmetic, with no reset between transactions.</p><div class="chip-row">${Object.entries(
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
              `<div class="bar-row"><span>${label}</span><span class="bar-track"><span class="bar-fill" style="width:${max > 0 && value > 0 ? (value / max) * 100 : 0}%;${label === "Sanity" ? "background:var(--green)" : ""}"></span></span><span class="bar-num">${int(value)}</span></div>`,
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
          )}</tbody></table><div class="chip-row">${(p.directed || []).map((d) => `<span class="chip">${esc(d)}</span>`).join("")}</div></details><div class="pc-links"><a href="${src}/dut.py" target="_blank" rel="noopener">View executable spec ↗</a><a href="${src}/baseline.v" target="_blank" rel="noopener">Baseline RTL ↗</a></div></article>`;
      })
      .join("");
    const target = document.getElementById(
      decodeURIComponent(location.hash.slice(1)),
    );
    if (target) target.scrollIntoView();
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
      "#pilot-note",
      `${m.repetitions || 1} attempt(s) per model–problem pair · ${m.budget_s ? m.budget_s / 60 + " min budget" : "budget unreported"} · ${m.sandbox?.mode || "sandbox unreported"}. Cells × cycles is an area–delay proxy, not a power or physical-timing measurement. Small pilot; model variance is not yet established.`,
    );
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
      const [leaderboard, problems] = await Promise.all([
        getJSON("data/leaderboard.json"),
        getJSON("data/problems.json").catch(() => null),
      ]);
      if (
        !Array.isArray(leaderboard.models) ||
        !Array.isArray(problems?.problems || leaderboard.problems)
      )
        throw new Error("Invalid published dataset");
      data = {
        ...leaderboard,
        meta: leaderboard.meta || {},
        problems: problems?.problems || leaderboard.problems,
      };
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
