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

Deployment: `.github/workflows/deploy-site.yml` runs on push to `main`. It
always uploads the site as a downloadable workflow artifact, and it deploys to
GitHub Pages when Pages is enabled and the `ENABLE_PAGES=true` repository
variable is set. GitHub Pages is free for public repositories; a private
repository needs a paid plan, in which case deploy the artifact to any static
host instead.

Design references: model × problem heatmaps (KernelBench), KPI strip and
per-column aggregates (LiveBench), tab navigation, filters, and persisted
theme (SWE-bench), dark data-dense tables (Artificial Analysis).
