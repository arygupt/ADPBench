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

Design reference: [DeepSWE](https://deepswe.datacurve.ai/), adapted around
ADPBench's hardware metrics. The interface uses neutral surfaces, restrained
violet accents, Inter and JetBrains Mono, and a persistent light/dark preference.

The results view includes model search, sorting, a problem matrix, and a native
modal run inspector with complete artifact hashes. All repetitions are visible
in matrix cells. Confidence intervals display the actual 95% Wilson lower and
upper bounds, rather than a symmetric ± around an asymmetric interval.

The area/latency chart compares only correct runs against the selected problem's
baseline on logarithmic axes. Chart points and legend buttons open run details;
the legend also makes nearly overlapping points independently accessible.
Infrastructure failures and incorrect RTL follow the report's classification.
ADP improvements are never labeled as speedups. Data and metadata come from the
committed JSON; no benchmark jobs run in the browser.

The catalog exposes numeric contracts, edge cases, reference RTL, and baseline
versus sanity ADP. Methodology has section navigation and copyable code blocks.
There is no package installation or frontend build step. Check changes with the
site/report unit tests and browser checks at desktop and phone widths, including
search, sorting, matrix cells, chart selection, dialog keyboard behavior, and
both themes.
