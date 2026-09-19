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
  const colors = [
    "#7958bb",
    "#398268",
    "#b67c38",
    "#577dba",
    "#a66683",
    "#528d91",
  ];
  const symbols = ["a·b", "Ax", "AB", "f∗g"];
  let data, dialog, returnFocus;
  const color = (model) => colors[data.models.indexOf(model) % colors.length];
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
      return { cls: "infra", label: "Infrastructure", value: "Infra" };
    return { cls: "fail", label: "Incorrect RTL", value: "Failed" };
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
  function modelName(model) {
    return `<span class="model-name" title="${esc(model.label)}"><span class="model-symbol" style="--model-color:${color(model)}">${esc(short(model.label).slice(0, 2).toUpperCase())}</span><span><strong>${esc(short(model.label))}</strong><small>${esc(model.label.split("/")[0])}${model.label.endsWith("-free") ? " · free tier" : ""}</small></span></span>`;
  }
  function renderStats() {
    const runs = data.models.flatMap((m) => m.runs),
      correct = runs.filter((r) => r.correct),
      best = Math.max(0, ...correct.map((r) => r.ratio));
    const stats = [
      [data.problems.length, "Numeric operators"],
      [data.models.length, "Evaluated models"],
      [runs.length, "Recorded attempts"],
      [ratio(best), "Best ADP improvement"],
    ];
    if ($("#stat-strip"))
      $("#stat-strip").innerHTML = stats
        .map(
          ([v, l]) =>
            `<div class="stat"><div class="stat-value">${v}</div><div class="stat-label">${l}</div></div>`,
        )
        .join("");
    fill("#dataset-pilot", data.meta.pilot || "Published pilot");
    fill(
      "#dataset-summary",
      `${data.meta.repetitions || 1} attempt${data.meta.repetitions > 1 ? "s" : ""} / pair · ${data.meta.budget_s ? data.meta.budget_s / 60 + " min budget" : "budget not reported"} · ${data.meta.sandbox?.mode || "sandbox not reported"}`,
    );
    fill(
      "#dataset-date",
      `Snapshot ${data.meta.generated?.slice(0, 10) || "undated"}`,
    );
  }
  function renderResults() {
    if (!$("#leaderboard-rows")) return;
    const query = $("#model-search").value.trim().toLowerCase(),
      sort = $("#model-sort").value;
    const all = ranked(data.models).sort((a, b) => b[sort] - a[sort]);
    const models = all.filter((m) => m.label.toLowerCase().includes(query));
    fill(
      "#leaderboard-count",
      `${models.length} of ${data.models.length} models · sorted by ${$("#model-sort").selectedOptions[0].textContent.toLowerCase()}`,
    );
    $("#leaderboard-rows").innerHTML =
      models
        .map((model) => {
          const [low, high] = wilson(model.beating, model.attempts);
          return `<tr style="--model-color:${color(model)}"><td class="lb-rank">${String(all.indexOf(model) + 1).padStart(2, "0")}</td><td>${modelName(model)}</td><td><div class="score-top"><strong>${pct(model.beat_rate)}</strong><small title="95% Wilson confidence interval">${pct(low)}–${pct(high)} CI</small></div><span class="score-track" aria-hidden="true"><span class="score-fill" style="width:${model.beat_rate * 100}%"></span><span class="score-interval" style="left:${low * 100}%;width:${(high - low) * 100}%"></span></span></td><td><span class="metric-value">${pct(model.correctness_rate)}</span><span class="metric-caption">${model.correct} / ${model.attempts} attempts</span></td><td><span class="metric-value">${ratio(model.geomean)}</span><span class="metric-caption">${model.correct ? "correct-run geomean" : "no correct runs"}</span></td><td><button class="row-open" data-model="${esc(model.label)}" aria-label="Inspect ${esc(short(model.label))} runs">↗</button></td></tr>`;
        })
        .join("") ||
      '<tr><td colspan="6" class="empty-state">No models match this search. Try a different name.</td></tr>';
    $("#heatmap").innerHTML =
      `<table><thead><tr><th scope="col">Evaluated model</th>${data.problems.map((p) => `<th scope="col">${esc(p.title)}</th>`).join("")}</tr></thead><tbody>${
        models
          .map(
            (model) =>
              `<tr><th scope="row">${esc(short(model.label))}</th>${data.problems
                .map((p) => {
                  const runs = model.runs.filter((r) => r.problem === p.name);
                  return `<td>${
                    runs.length
                      ? runs
                          .map((r) => {
                            const s = state(r);
                            return `<button class="hm-cell ${s.cls}" data-model="${esc(model.label)}" data-problem="${esc(p.name)}" aria-label="${esc(short(model.label))}, ${esc(p.title)}, attempt ${r.attempt}: ${s.label}, ${s.value}">${s.value}${runs.length > 1 ? ` <small>#${r.attempt}</small>` : ""}</button>`;
                          })
                          .join("")
                      : '<span class="hm-cell none">Not run</span>'
                  }</td>`;
                })
                .join("")}</tr>`,
          )
          .join("") ||
        '<tr><td colspan="5" class="empty-state">No models match this search.</td></tr>'
      }</tbody></table><div class="legend"><span>● ADP ratio &gt; 1×: beats baseline</span><span>≤ 1×: correct, no improvement</span><span>Failed: incorrect RTL</span><span>Infra: no scored result</span></div>`;
  }
  function renderOutcomes() {
    if (!$("#outcomes")) return;
    const runs = data.models.flatMap((m) => m.runs);
    const groups = [
      ["beat", "Beat baseline", "var(--green)"],
      ["correct", "Correct, no ADP improvement", "var(--orange)"],
      ["fail", "Incorrect RTL", "var(--red)"],
      ["infra", "Infrastructure failure", "var(--faint)"],
    ].map(([key, label, c]) => ({
      key,
      label,
      c,
      n: runs.filter((r) => state(r).cls === key).length,
    }));
    $("#outcomes").innerHTML =
      `<div class="outcome-bar" aria-hidden="true">${groups
        .filter((g) => g.n)
        .map((g) => `<span style="flex:${g.n};background:${g.c}"></span>`)
        .join(
          "",
        )}</div>${groups.map((g) => `<div class="outcome-row"><span class="swatch" style="background:${g.c}"></span><span>${g.label}</span><strong>${g.n}<small>${pct(runs.length ? g.n / runs.length : 0)}</small></strong></div>`).join("")}<p class="outcome-foot">${runs.filter((r) => r.correct).length} correct designs across ${runs.length} recorded attempts. Timeouts with a scored design retain that design’s outcome.</p>`;
  }
  function renderChart() {
    const root = $("#tradeoff-chart");
    if (!root) return;
    const p = data.problems.find((p) => p.name === $("#chart-problem").value);
    if (!p || !(p.baseline?.cells > 0 && p.baseline?.cycles > 0)) {
      root.innerHTML =
        '<p class="empty-state">No baseline available for comparison.</p>';
      fill("#chart-legend", "");
      return;
    }
    const points = data.models.flatMap((m) =>
      m.runs
        .filter(
          (r) =>
            r.problem === p.name && r.correct && r.cells > 0 && r.cycles > 0,
        )
        .map((r) => ({
          model: m,
          run: r,
          x: r.cells / p.baseline.cells,
          y: r.cycles / p.baseline.cycles,
        })),
    );
    if (!points.length) {
      root.innerHTML =
        '<div class="empty-state"><strong>No correct designs yet.</strong><p>No correct, scored runs are available for this operator. Explore the problem matrix for run details.</p></div>';
      fill("#chart-legend", "");
      return;
    }
    const xmin = Math.min(
        -1,
        Math.floor(Math.log10(Math.min(1, ...points.map((p) => p.x)))),
      ),
      xmax = Math.max(
        1,
        Math.ceil(Math.log10(Math.max(1, ...points.map((p) => p.x)))),
      );
    const ymin = Math.min(
        -1,
        Math.floor(Math.log10(Math.min(1, ...points.map((p) => p.y)))),
      ),
      ymax = Math.max(
        0,
        Math.ceil(Math.log10(Math.max(1, ...points.map((p) => p.y)))),
      );
    const X = (n) => 55 + ((Math.log10(n) - xmin) / (xmax - xmin)) * 395,
      Y = (n) => 210 - ((Math.log10(n) - ymin) / (ymax - ymin)) * 185;
    let grid = "";
    for (let i = xmin; i <= xmax; i++) {
      const x = X(10 ** i);
      grid += `<line x1="${x}" y1="25" x2="${x}" y2="210" class="${i === 0 ? "chart-baseline" : "chart-grid"}"/><text x="${x}" y="230" text-anchor="middle">${10 ** i}×</text>`;
    }
    for (let i = ymin; i <= ymax; i++) {
      const y = Y(10 ** i);
      grid += `<line x1="55" y1="${y}" x2="450" y2="${y}" class="${i === 0 ? "chart-baseline" : "chart-grid"}"/><text x="45" y="${y + 3}" text-anchor="end">${10 ** i}×</text>`;
    }
    root.innerHTML = `<svg class="chart" viewBox="0 0 490 270" role="group" aria-label="${esc(p.title)}: cell count and cycles relative to baseline"><text x="55" y="12">CYCLES / BASELINE</text>${grid}<text x="255" y="258" text-anchor="middle">CELLS / BASELINE →</text><path d="M${X(1) - 4} ${Y(1) - 4}l8 8m0-8l-8 8" stroke="var(--text)" stroke-width="2"/><text x="${X(1) + 10}" y="${Y(1) + 12}">Baseline</text>${points.map((pt, i) => `<g class="chart-point" role="button" tabindex="0" data-model="${esc(pt.model.label)}" data-problem="${esc(p.name)}" aria-label="${esc(short(pt.model.label))}, attempt ${pt.run.attempt}: ${ratio(pt.run.ratio)} ADP improvement, ${ratio(pt.x)} cells, ${ratio(pt.y)} cycles"><title>${esc(short(pt.model.label))}: ${int(pt.run.cells)} cells · ${int(pt.run.cycles)} cycles · ${ratio(pt.run.ratio)} ADP improvement</title><circle cx="${X(pt.x)}" cy="${Y(pt.y)}" r="7" fill="${color(pt.model)}" stroke="var(--surface)" stroke-width="2"/><text x="${X(pt.x) + 10}" y="${Y(pt.y) + (i % 2 ? 17 : -9)}">${i + 1}</text></g>`).join("")}</svg>`;
    $("#chart-legend").innerHTML = points
      .map(
        (pt, i) =>
          `<button class="chart-legend-button" data-model="${esc(pt.model.label)}" data-problem="${esc(p.name)}" aria-label="Inspect ${esc(short(pt.model.label))} on ${esc(p.title)}"><i class="swatch" style="background:${color(pt.model)}"></i>${i + 1}. ${esc(short(pt.model.label))}</button>`,
      )
      .join("");
  }
  function openRuns(label, problem) {
    const model = data.models.find((m) => m.label === label);
    if (!model) return;
    returnFocus = document.activeElement;
    const runs = model.runs.filter((r) => !problem || r.problem === problem);
    dialog.innerHTML = `<div class="dialog-head"><div><p class="eyebrow">FROZEN RUN RECORDS · ${esc(data.meta.pilot)}</p><h2 id="dialog-title">${esc(short(label))}</h2></div><button class="dialog-close" aria-label="Close run details" autofocus>×</button></div><div class="dialog-body"><div class="dialog-summary"><span class="badge">${model.beating}/${model.attempts} beat baseline</span><span class="badge">${model.correct}/${model.attempts} correct</span><span class="badge">${model.wrong_rtl} wrong RTL</span><span class="badge">${model.infra} infrastructure</span></div><div class="run-grid">${runs
      .map((r) => {
        const s = state(r);
        return `<article class="run-card"><div class="rc-head"><strong>${esc(title(r.problem))}</strong><span class="rc-status ${s.cls}">${s.label}</span></div><p class="rc-meta">Attempt ${r.attempt} · ${int(Math.round(r.duration_s))}s · ${int(r.history)} checks${r.timed_out ? " · agent timed out" : ""}</p><div class="rc-ratio">${r.correct ? ratio(r.ratio) : "—"}</div><p class="metric-caption">${r.correct ? "baseline ADP / design ADP" : "No valid ADP score"}</p><p class="rc-meta">${int(r.cells)} cells × ${int(r.cycles)} cycles<br>ADP ${int(r.adp)}<br>Stage: ${esc(r.stage || "not reported")} · audit ${r.audit_ok ? "passed" : "not passed"}</p>${r.correctness || r.error ? `<p class="rc-detail">${esc(r.correctness || r.error)}</p>` : ""}<details><summary>Inspect artifact hashes</summary><p>Submission SHA-256</p><p class="hash-value">${esc(r.submission_sha256 || "Not recorded")}</p><p>Netlist SHA-256</p><p class="hash-value">${esc(r.netlist_sha256 || "Not recorded")}</p></details></article>`;
      })
      .join(
        "",
      )}</div><p class="pilot-note">Ratios are area–delay improvements, not clock-speed measurements. <a href="data/leaderboard.json">Read the source records ↗</a></p></div>`;
    $(".dialog-close", dialog).addEventListener("click", () => dialog.close());
    dialog.showModal();
    document.body.style.overflow = "hidden";
  }
  function renderProblems() {
    if ($("#problem-previews"))
      $("#problem-previews").innerHTML = data.problems
        .map((p, i) => {
          const runs = data.models
              .flatMap((m) => m.runs)
              .filter((r) => r.problem === p.name),
            correct = runs.filter((r) => r.correct).length;
          return `<a class="problem-preview" href="problems.html#${esc(p.name)}"><span class="preview-symbol">${symbols[i] || "ƒ"}</span><h3>${esc(p.title)}</h3><p>${p.params.DATA_W}-bit inputs · ${p.params.LANES} lanes</p><span class="preview-foot"><span>${correct}/${runs.length} correct attempts</span><span aria-hidden="true">↗</span></span></a>`;
        })
        .join("");
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
    const m = data.meta;
    fill("#meta-generated", m.generated?.slice(0, 10) || "—");
    fill("#meta-commit", m.git_commit?.slice(0, 10) || "—");
    fill("#meta-pilot", m.pilot || "—");
    fill("#meta-budget", m.budget_s ? `${m.budget_s}s` : "—");
    fill("#meta-yosys", m.tools?.yosys || "—");
    fill("#meta-iverilog", m.tools?.iverilog || "—");
    fill(
      "#meta-seeds",
      `Held-out seeds: ${(m.cases?.seeds || []).join(", ")} · ${m.cases?.transactions || "—"} transactions`,
    );
  }
  function setupShell() {
    const main = $("main");
    if (main) {
      main.id = "main";
      const skip = document.createElement("a");
      skip.href = "#main";
      skip.className = "skip-link";
      skip.textContent = "Skip to content";
      document.body.prepend(skip);
    }
    const actions = $(".header-actions");
    if (actions) {
      const button = document.createElement("button");
      button.className = "theme-toggle";
      button.type = "button";
      const update = () => {
        const dark = document.documentElement.dataset.theme === "dark";
        button.textContent = dark ? "☼" : "◐";
        button.setAttribute(
          "aria-label",
          dark ? "Switch to light theme" : "Switch to dark theme",
        );
      };
      update();
      button.addEventListener("click", () => {
        const theme =
          document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = theme;
        try {
          localStorage.setItem("adpbench-theme", theme);
        } catch (_) {}
        update();
      });
      actions.append(button);
    }
    $$(".site-nav a.active").forEach((a) =>
      a.setAttribute("aria-current", "page"),
    );
    $$(".prose pre").forEach((pre) => {
      const text = pre.textContent,
        button = document.createElement("button");
      button.className = "copy-button";
      button.textContent = "Copy";
      button.setAttribute("aria-label", "Copy code block");
      button.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          button.textContent = "Copied";
        } catch (_) {
          button.textContent = "Select code to copy";
        }
        setTimeout(() => (button.textContent = "Copy"), 2000);
      });
      pre.before(button);
    });
    dialog = document.createElement("dialog");
    dialog.setAttribute("aria-labelledby", "dialog-title");
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      document.body.style.overflow = "";
      returnFocus?.focus();
    });
    document.addEventListener("click", (e) => {
      const button = e.target.closest("[data-model]");
      if (button) openRuns(button.dataset.model, button.dataset.problem);
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.matches(".chart-point") && ["Enter", " "].includes(e.key)) {
        e.preventDefault();
        openRuns(e.target.dataset.model, e.target.dataset.problem);
      }
    });
    $$("[data-view]").forEach((button) =>
      button.addEventListener("click", () => {
        $$("[data-view]").forEach((b) => {
          const selected = b === button;
          b.setAttribute("aria-pressed", String(selected));
          $(`#${b.dataset.view}-view`).hidden = !selected;
        });
      }),
    );
    $("#model-search")?.addEventListener("input", renderResults);
    $("#model-sort")?.addEventListener("change", renderResults);
    $("#chart-problem")?.addEventListener("change", renderChart);
  }
  async function getJSON(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`Could not load ${path} (${r.status})`);
    return r.json();
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
      data.models = ranked(data.models);
      renderStats();
      renderResults();
      renderOutcomes();
      renderProblems();
      renderMeta();
      if ($("#chart-problem")) {
        $("#chart-problem").innerHTML = data.problems
          .map((p) => `<option value="${esc(p.name)}">${esc(p.title)}</option>`)
          .join("");
        renderChart();
      }
    } catch (error) {
      const root = $("#stat-strip") || $("#problem-grid") || $("main");
      root.insertAdjacentHTML(
        "afterbegin",
        '<div class="error-state" role="alert"><strong>Published results couldn’t load.</strong><p>Refresh to try again, or <a href="data/report.json">open the report directly</a>.</p></div>',
      );
      $$(".loading").forEach((el) => el.remove());
      console.error(error);
    }
  });
})();
