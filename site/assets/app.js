/* ADPBench site renderer. Zero dependencies. Leaderboard bars follow
   DeepSWE's design: rank-colored 6px bars, score + Wilson CI, badge chips. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const MODEL_COLORS = ["#6366f1", "#22c55e", "#f59e0b", "#06b6d4", "#a78bfa",
    "#f43f5e", "#84cc16", "#818cf8", "#14b8a6", "#c084fc"];
  const PALETTE = new Map();

  const fmt = {
    int: (n) => n.toLocaleString("en-US"),
    pct: (x) => `${Math.round(x * 100)}%`,
    ratio: (x) => (x > 0 ? `${x.toFixed(2)}×` : "—"),
    adp: (x) => (x > 0 ? fmt.int(Math.round(x)) : "—"),
    sha: (s) => (s ? s.slice(0, 10) : ""),
    secs: (s) => (s > 0 ? `${Math.round(s)}s` : ""),
  };

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

  /* ------------------------------------------------------------ stats */

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
    const stats = [
      { value: String(data.problems.length), label: "Problems" },
      { value: String(models.length), label: "Evaluated systems" },
      { value: String(runs.length), label: "Sandboxed runs" },
      { value: fmt.pct(correct / runs.length), label: "Correct" },
      { value: fmt.ratio(best), label: "Best speedup" },
      { value: fmt.ratio(geomean), label: "Geomean, successful" },
    ];
    strip.innerHTML = stats.map(
      (s) => `<div class="stat"><div class="stat-value">${s.value}</div><div class="stat-label">${s.label}</div></div>`
    ).join("");
  }

  /* ------------------------------------------------------------ leaderboard */

  function renderLeaderboard(data) {
    const tbody = $("#leaderboard-rows");
    const count = $("#leaderboard-count");
    if (!tbody) return;
    if (count) count.textContent = `${data.models.length} / ${data.models.length} models`;
    const ranked = rankedModels(data.models);
    tbody.innerHTML = ranked.map((model, index) => {
      const color = colorFor(model.label);
      const rankCls = index === 0 ? "lb-rank-1" : index === 1 ? "lb-rank-2" : index === 2 ? "lb-rank-3" : "lb-other";
      const scorePct = Math.round(model.beat_rate * 100);
      const tier = /free/.test(model.label) ? "free" : "";
      const geomean = model.geomean > 0 ? fmt.ratio(model.geomean) : "";
      return `<div class="leaderboard-row ${rankCls}" data-model="${escapeHtml(model.label)}">
        <span class="lb-rank${index < 3 ? " top" : ""}">${index + 1}</span>
        <span class="lb-model">
          <span class="model-dot" style="background:${color}"></span>
          ${escapeHtml(model.label)}
          ${tier ? `<span class="badge">${tier}</span>` : ""}
        </span>
        <span class="lb-score">${scorePct}% <span class="lb-ci">${model.score_ci || ""}</span></span>
        <span class="lb-bar-wrap"><span class="lb-bar"><span class="lb-bar-fill" style="width:${scorePct}%"></span></span></span>
        <span style="display:none"></span>
      </div>
      <div class="detail-panel hidden" data-detail="${escapeHtml(model.label)}">
        <div class="panel-title">${escapeHtml(model.label)} — per-problem runs · correct ${model.correct}/${model.attempts} · ${geomean ? "geomean " + geomean : "no successful runs"} · ${model.wrong_rtl} wrong · ${model.infra} infra</div>
        <div class="run-grid">${renderRunCards(model, data)}</div>
      </div>`;
    }).join("");

    tbody.querySelectorAll(".leaderboard-row").forEach((row) => {
      row.addEventListener("click", () => toggleDetail(row.dataset.model));
    });
    $$(".hm-cell", document).forEach((el) => {
      el.addEventListener("click", () => openDetail(el.dataset.model));
    });
  }

  function renderRunCards(model, data) {
    const order = data.problems.map((p) => p.name);
    const runs = [...model.runs].sort(
      (a, b) => order.indexOf(a.problem) - order.indexOf(b.problem)
    );
    return runs.map((run) => {
      const state = cellState(run);
      const badge = run.correct
        ? `<span class="rc-status ok">correct</span>`
        : run.error || run.stage === "no result" || (run.timed_out && !run.stage)
          ? `<span class="rc-status inf">infra</span>`
          : run.stage
            ? `<span class="rc-status fail">failed</span>`
            : `<span class="rc-status none">no submission</span>`;
      const meta = run.cells > 0
        ? `${fmt.int(run.cells)} cells × ${fmt.int(run.cycles)} cycles = ${fmt.adp(run.adp)} adp`
        : run.timed_out ? "agent timed out" : "—";
      const hash = run.submission_sha256
        ? `submission ${fmt.sha(run.submission_sha256)} · netlist ${fmt.sha(run.netlist_sha256)}`
        : "";
      const detail = run.correctness ? escapeHtml(run.correctness.slice(0, 110)) : "";
      return `<div class="run-card">
        <div class="rc-head">
          <span class="rc-problem">${escapeHtml(problemTitle(data, run.problem))}</span>
          <span class="rc-ratio" style="color:${run.correct && run.ratio > 1 ? "var(--green)" : "var(--text)"}">${state.text}</span>
        </div>
        <div class="rc-meta">${meta}${hash ? `<br>${hash}` : ""}</div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          ${badge}
          <span style="color:var(--text-tertiary);font-size:11.5px">${fmt.secs(run.duration_s)} · ${run.history} tested</span>
        </div>
        ${detail ? `<div style="color:var(--text-tertiary);font-size:11.5px;margin-top:8px">${detail}</div>` : ""}
      </div>`;
    }).join("");
  }

  function toggleDetail(modelLabel) {
    const panel = document.querySelector(`.detail-panel[data-detail="${cssEscape(modelLabel)}"]`);
    const row = document.querySelector(`.leaderboard-row[data-model="${cssEscape(modelLabel)}"]`);
    if (!panel || !row) return;
    const open = panel.classList.toggle("hidden");
    row.classList.toggle("open", !open);
  }

  function openDetail(modelLabel) {
    const panel = document.querySelector(`.detail-panel[data-detail="${cssEscape(modelLabel)}"]`);
    if (panel && panel.classList.contains("hidden")) toggleDetail(modelLabel);
    const row = document.querySelector(`.leaderboard-row[data-model="${cssEscape(modelLabel)}"]`);
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
      return `<tr><th><span class="lb-model"><span class="model-dot" style="background:${colorFor(model.label)}"></span>${escapeHtml(model.label)}</span></th>${cells}</tr>`;
    }).join("");
    wrap.innerHTML = `<table>${head}${body}</table>
      <div class="legend">
        <span><span class="swatch" style="background:var(--green)"></span>beat baseline (&gt;1×)</span>
        <span><span class="swatch" style="background:var(--orange)"></span>correct ≤ 1× / infra</span>
        <span><span class="swatch" style="background:var(--red)"></span>incorrect</span>
        <span><span class="swatch" style="background:var(--text-tertiary)"></span>no submission</span>
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
        <h3>${escapeHtml(p.title)} <span class="badge">${escapeHtml(p.name)}</span><span class="badge" style="background:var(--bg-hover);color:var(--text-tertiary)">level ${p.level}</span></h3>
        <div class="chip-row">${ports}</div>
        <div class="pc-desc">${fmt.int(p.transactions)} back-to-back transactions · ${fmt.int(p.out_len)} output word${p.out_len === 1 ? "" : "s"} per transaction · lanes = ${p.params.LANES}, data = ${p.params.DATA_W} bit, acc = ${p.params.ACC_W} bit</div>
        <div class="chip-row">${quant}</div>
        <div class="chip-row">${directed ? `<span class="chip">directed: ${directed}</span>` : ""}</div>
        <div class="bar-pair">
          <div class="bar-row"><span>baseline</span><span class="bar-track"><span class="bar-fill" style="width:100%;background:var(--text-tertiary)"></span></span><span class="bar-num">${fmt.int(baseline.cells || 0)}c · ${fmt.int(baseline.cycles || 0)}cy</span></div>
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
    const { leaderboard, problems } = await loadData();
    const data = { ...leaderboard, problems: problems ? problems.problems : leaderboard.problems };
    renderStatStrip(data);
    renderLeaderboard(data);
    renderHeatmap(data);
    renderProblems(data);
    renderMeta(data);
  });
})();
