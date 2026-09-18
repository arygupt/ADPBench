/* ADPBench site renderer. Zero dependencies. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const MODEL_COLORS = ["#4f8cff", "#9d7bff", "#22c5c5", "#f08c3a", "#e0568b",
    "#7cb342", "#f4c542", "#5c7cfa", "#20b2aa", "#b56bff"];
  const PALETTE = new Map();

  const fmt = {
    int: (n) => n.toLocaleString("en-US"),
    pct: (x) => `${Math.round(x * 100)}%`,
    ratio: (x) => (x > 0 ? `${x.toFixed(2)}×` : "—"),
    adp: (x) => (x > 0 ? fmt.int(Math.round(x)) : "—"),
    sha: (s) => (s ? s.slice(0, 10) : ""),
    secs: (s) => (s > 0 ? `${Math.round(s)}s` : ""),
  };

  /* ------------------------------------------------------------ theme */

  const root = document.documentElement;
  const themeToggle = $("#theme-toggle");
  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem("adpbench-theme", theme); } catch (e) { /* ignore */ }
    if (themeToggle) {
      themeToggle.textContent = theme === "dark" ? "☀️" : "🌙";
      themeToggle.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    }
  }
  if (themeToggle) {
    themeToggle.addEventListener("click", () =>
      applyTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark")
    );
  }

  /* ------------------------------------------------------------ data */

  async function loadData() {
    const [leaderboard, problems] = await Promise.all([
      fetch("data/leaderboard.json").then((r) => r.json()),
      fetch("data/problems.json").then((r) => r.json()).catch(() => null),
    ]);
    return { leaderboard, problems };
  }

  function problemTitle(data, name) {
    const problem = data.problems.find((p) => p.name === name);
    return problem ? problem.title : name;
  }

  function colorFor(label) {
    if (!PALETTE.has(label)) {
      PALETTE.set(label, MODEL_COLORS[PALETTE.size % MODEL_COLORS.length]);
    }
    return PALETTE.get(label);
  }

  /* rank: beat-baseline rate, then correctness rate, then geomean */
  function rankedModels(models) {
    return [...models].sort((a, b) =>
      b.beat_rate - a.beat_rate ||
      b.correctness_rate - a.correctness_rate ||
      b.geomean - a.geomean
    );
  }

  function cellState(run) {
    if (run.correct && run.ratio > 1.0) return { cls: "beat", text: fmt.ratio(run.ratio) };
    if (run.correct) return { cls: "correct", text: fmt.ratio(run.ratio) };
    if (run.error || run.stage === "no result" || (run.timed_out && !run.stage))
      return { cls: "correct", text: "⚠" };
    if (run.stage) return { cls: "fail", text: "✗" };
    return { cls: "none", text: "—" };
  }

  function cellTitle(run) {
    const parts = [];
    if (run.correct) parts.push(`correct, ${fmt.ratio(run.ratio)}`);
    else if (run.error) parts.push(`infrastructure: ${run.error.slice(0, 60)}`);
    else if (run.correctness) parts.push(run.correctness.slice(0, 80));
    else parts.push("no submission");
    if (run.cells > 0) parts.push(`${fmt.int(run.cells)} cells × ${fmt.int(run.cycles)} cycles`);
    if (run.timed_out) parts.push("agent timed out");
    return parts.join(" · ");
  }

  /* ------------------------------------------------------------ leaderboard */

  function renderStatStrip(data) {
    const strip = $("#stat-strip");
    if (!strip) return;
    const models = data.models;
    const runs = models.flatMap((m) => m.runs);
    const correct = runs.filter((r) => r.correct).length;
    const best = Math.max(0, ...runs.map((r) => r.ratio || 0));
    const geomeans = models.map((m) => m.geomean).filter((g) => g > 0);
    const geomean = geomeans.length
      ? Math.exp(geomeans.reduce((s, g) => s + Math.log(g), 0) / geomeans.length)
      : 0;
    const beating = runs.filter((r) => r.correct && r.ratio > 1).length;
    const stats = [
      { value: String(data.problems.length), label: "problems" },
      { value: String(models.length), label: "evaluated systems" },
      { value: String(runs.length), label: "sandboxed runs" },
      { value: fmt.pct(correct / runs.length), label: "correct" },
      { value: fmt.ratio(best), label: "best speedup" },
      { value: fmt.ratio(geomean), label: "geomean, successful" },
    ];
    strip.innerHTML = stats.map(
      (s) => `<div class="stat"><div class="value">${s.value}</div><div class="label">${s.label}</div></div>`
    ).join("");
    const beatEl = $("#kpi-beating");
    if (beatEl) beatEl.textContent = `${beating}/${runs.length}`;
  }

  function renderLeaderboard(data) {
    const tbody = $("#leaderboard tbody");
    if (!tbody) return;
    const problems = data.problems.map((p) => p.name);
    const ranked = rankedModels(data.models);
    tbody.innerHTML = ranked.map((model, index) => {
      const color = colorFor(model.label);
      const cells = problems.map((name) => {
        const run = model.runs.find((r) => r.problem === name);
        const state = run ? cellState(run) : { cls: "none", text: "—" };
        return `<td class="num"><span class="cell ${state.cls}" title="${run ? cellTitle(run) : "not run"}" data-model="${model.label}" data-problem="${name}">${state.text}</span></td>`;
      }).join("");
      const beatPct = Math.round(model.beat_rate * 100);
      const rankCls = index < 3 ? "rank top" : "rank";
      return `<tr class="clickable" data-model="${model.label}">
        <td class="${rankCls}">${index + 1}</td>
        <td><span class="model-chip"><span class="model-dot" style="background:${color}"></span>${escapeHtml(model.label)}</span></td>
        <td class="num">${fmt.pct(model.correctness_rate)}</td>
        <td class="num"><span class="meter"><span class="track"><span class="fill" style="width:${beatPct}%;background:${beatPct > 0 ? "var(--green)" : "transparent"}"></span></span>${fmt.pct(model.beat_rate)}</span></td>
        <td class="num mono">${fmt.ratio(model.geomean)}</td>
        ${cells}
        <td class="num">${model.infra > 0 ? `<span title="infrastructure failures">${model.infra}</span>` : "—"}</td>
        <td class="num"><span class="chev">▶</span></td>
      </tr>
      <tr class="detail-panel hidden" data-detail="${model.label}"><td colspan="${problems.length + 8}">
        <div class="panel-title">${escapeHtml(model.label)} — per-problem runs</div>
        <div class="run-grid">${renderRunCards(model, problems, data)}</div>
      </td></tr>`;
    }).join("");

    tbody.querySelectorAll("tr.clickable").forEach((row) => {
      row.addEventListener("click", () => toggleDetail(row.dataset.model));
    });
    tbody.querySelectorAll(".cell, .hm-cell").forEach((el) => {
      el.addEventListener("click", (event) => {
        event.stopPropagation();
        openDetail(el.dataset.model);
      });
    });
  }

  function renderRunCards(model, problems, data) {
    const order = problems.map((p) => p.name);
    const runs = [...model.runs].sort(
      (a, b) => order.indexOf(a.problem) - order.indexOf(b.problem)
    );
    return runs.map((run) => {
      const state = cellState(run);
      const badge = run.correct
        ? `<span class="rc-status ok">correct</span>`
        : run.error || run.stage === "no result"
          ? `<span class="rc-status inf">infra</span>`
          : run.stage
            ? `<span class="rc-status fail">failed</span>`
            : `<span class="rc-status none">no submission</span>`;
      const meta = run.cells > 0
        ? `${fmt.int(run.cells)} cells × ${fmt.int(run.cycles)} cycles = ${fmt.adp(run.adp)} adp`
        : run.timed_out ? "agent timed out" : "—";
      const detail = run.correctness ? escapeHtml(run.correctness.slice(0, 110)) : "";
      const hash = run.submission_sha256
        ? `submission ${fmt.sha(run.submission_sha256)} · netlist ${fmt.sha(run.netlist_sha256)}`
        : "";
      return `<div class="run-card">
        <div class="rc-head">
          <span class="rc-problem">${escapeHtml(problemTitle(data, run.problem))}</span>
          <span class="rc-ratio" style="color:${run.correct && run.ratio > 1 ? "var(--green)" : "var(--text)"}">${state.text}</span>
        </div>
        <div class="rc-meta">${meta}${hash ? `<br>${hash}` : ""}</div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          ${badge}
          <span style="color:var(--text-faint);font-size:12px">${fmt.secs(run.duration_s)} · ${run.history} tested</span>
        </div>
        ${detail ? `<div style="color:var(--text-faint);font-size:12px;margin-top:8px">${detail}</div>` : ""}
      </div>`;
    }).join("");
  }

  function toggleDetail(modelLabel) {
    const panel = document.querySelector(`tr[data-detail="${cssEscape(modelLabel)}"]`);
    const row = document.querySelector(`tr[data-model="${cssEscape(modelLabel)}"]`);
    if (!panel || !row) return;
    const open = panel.classList.toggle("hidden");
    row.classList.toggle("open", !open);
  }

  function openDetail(modelLabel) {
    const panel = document.querySelector(`tr[data-detail="${cssEscape(modelLabel)}"]`);
    if (panel && panel.classList.contains("hidden")) toggleDetail(modelLabel);
    const row = document.querySelector(`tr[data-model="${cssEscape(modelLabel)}"]`);
    if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function renderHeatmap(data) {
    const wrap = $("#heatmap");
    if (!wrap) return;
    const problems = data.problems.map((p) => p.name);
    const ranked = rankedModels(data.models);
    const head = `<tr><th></th>${problems.map((p) => `<th>${escapeHtml(problemTitle(data, p))}</th>`).join("")}</tr>`;
    const body = ranked.map((model) => {
      const cells = problems.map((name) => {
        const run = model.runs.find((r) => r.problem === name);
        const state = run ? cellState(run) : { cls: "none", text: "—" };
        return `<td><div class="hm-cell ${state.cls}" title="${run ? cellTitle(run) : "not run"}" data-model="${model.label}" data-problem="${name}">${state.text}</div></td>`;
      }).join("");
      return `<tr><th><span class="model-chip"><span class="model-dot" style="background:${colorFor(model.label)}"></span>${escapeHtml(model.label)}</span></th>${cells}</tr>`;
    }).join("");
    wrap.innerHTML = `<table>${head}${body}</table>
      <div class="legend">
        <span><span class="swatch" style="background:var(--green)"></span>beat baseline (&gt;1×)</span>
        <span><span class="swatch" style="background:var(--amber)"></span>correct ≤ 1× / infra</span>
        <span><span class="swatch" style="background:var(--red)"></span>incorrect</span>
        <span><span class="swatch" style="background:var(--text-faint)"></span>no submission</span>
      </div>`;
    wrap.querySelectorAll(".hm-cell").forEach((el) => {
      el.addEventListener("click", () => openDetail(el.dataset.model));
    });
  }

  /* ------------------------------------------------------------ problems */

  function renderProblems(data) {
    const grid = $("#problem-grid");
    if (!grid) return;
    grid.innerHTML = data.problems.map((p) => {
      const baseline = p.baseline || {};
      const sanity = p.sanity || {};
      const ports = Object.entries(p.input_lens || {})
        .map(([port, len]) => `<span class="chip"><b>${escapeHtml(port)}</b> ${len}</span>`)
        .join("");
      const sanityWidth = sanity.ratio ? Math.round((1 / sanity.ratio) * 100) : 0;
      const quant = Object.entries(p.quant || {})
        .filter(([k]) => k !== "tolerance")
        .map(([k, v]) => `<span class="chip" title="${escapeHtml(v)}">${escapeHtml(k)}</span>`)
        .join("");
      const directed = (p.directed || []).map((d) => `<span class="chip">${escapeHtml(d)}</span>`).join("");
      return `<div class="problem-card">
        <h3>${escapeHtml(p.title)} <span class="badge">${escapeHtml(p.name)}</span><span class="badge" style="background:var(--gray-soft);color:var(--text-muted)">level ${p.level}</span></h3>
        <div class="chip-row">${ports}</div>
        <div class="pc-desc">${fmt.int(p.transactions)} back-to-back transactions · ${fmt.int(p.out_len)} output word${p.out_len === 1 ? "" : "s"} per transaction · lanes = ${p.params.LANES}, data = ${p.params.DATA_W} bit, acc = ${p.params.ACC_W} bit</div>
        <div class="chip-row">${quant}</div>
        <div class="chip-row">${directed ? `<span class="chip">directed: ${directed}</span>` : ""}</div>
        <div class="bar-pair">
          <div class="bar-row"><span>baseline</span><span class="bar-track"><span class="bar-fill" style="width:100%;background:var(--text-faint)"></span></span><span class="bar-num">${fmt.int(baseline.cells || 0)}c · ${fmt.int(baseline.cycles || 0)}cy</span></div>
          <div class="bar-row"><span>sanity</span><span class="bar-track"><span class="bar-fill" style="width:${sanityWidth}%;background:var(--green)"></span></span><span class="bar-num">${sanity.ratio ? fmt.ratio(sanity.ratio) : "—"}</span></div>
        </div>
      </div>`;
    }).join("");
  }

  /* ------------------------------------------------------------ meta */

  function renderMeta(data) {
    const meta = data.meta || {};
    const fill = (sel, value) => { const el = $(sel); if (el) el.textContent = value; };
    fill("#meta-commit", meta.git_commit ? meta.git_commit.slice(0, 12) : "—");
    fill("#meta-pilot", meta.pilot || "—");
    fill("#meta-yosys", (meta.tools && meta.tools.yosys) || "—");
    fill("#meta-iverilog", (meta.tools && meta.tools.iverilog) || "—");
    fill("#meta-generated", meta.generated || "—");
    fill("#meta-budget", meta.budget_s ? `${meta.budget_s}s per run` : "—");
    const seeds = $("#meta-seeds");
    if (seeds && meta.cases) {
      seeds.innerHTML = `<span class="chip">seeds ${(meta.cases.seeds || []).join(", ")}</span>
        <span class="chip">${meta.cases.transactions} transactions</span>`;
    }
  }

  /* ------------------------------------------------------------ helpers */

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function cssEscape(value) {
    return typeof CSS !== "undefined" && CSS.escape
      ? CSS.escape(value)
      : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  /* ------------------------------------------------------------ boot */

  document.addEventListener("DOMContentLoaded", async () => {
    let stored = null;
    try { stored = localStorage.getItem("adpbench-theme"); } catch (e) { /* ignore */ }
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    applyTheme(stored || (prefersDark ? "dark" : "dark"));

    const { leaderboard, problems } = await loadData();
    const data = { ...leaderboard, problems: problems ? problems.problems : leaderboard.problems };
    renderStatStrip(data);
    renderLeaderboard(data);
    renderHeatmap(data);
    renderProblems(data);
    renderMeta(data);
  });
})();
