# ADPBench site

A zero-dependency static leaderboard. `site/data/` is generated from frozen
pilot records by the harness:

```bash
adpbench site sanity --out pilot/sanity.json
adpbench site export --pilot runs/<pilot> --out site/data --sanity pilot/sanity.json
```

Preview locally:

```bash
python3 -m http.server 8000 --directory site
```

Deployment: the GitHub Actions workflow at
`.github/workflows/deploy-site.yml` publishes this directory to GitHub Pages
on every push to `main`. The site never touches the toolchain — it renders the
committed JSON.

Design references: model × problem heatmaps (KernelBench), KPI strip and
per-column aggregates (LiveBench), tab navigation, filters, and persisted
theme (SWE-bench), dark data-dense tables (Artificial Analysis).
