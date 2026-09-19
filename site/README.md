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

Layout reference: [kernelbench.com](https://kernelbench.com/). Typography and
colors match [Aryaman's personal site](https://www.aryamangupta.me/): Inter,
`#1a1a1a` charcoal, `#e7e5e4` off-white, and `#a8a29e` stone gray. Warm neutral
bars and labeled outcomes retain the compact results layout, minimal header,
and absence of a landing-page hero or methodology page. The first board ranks models by beat-baseline rate,
correctness, or correct-run geometric mean. The operator board switches between
ADP ratio, cells, and cycles. Search filters both boards.

Rate bars use the full 0–100% scale; Wilson confidence bounds are available in
the hover text. Geomean and operator ratio bars show each result as a share of
the best correct result. Cells/cycles bars use best/value (lower is better).
Stars identify ties for the best value, computed across the full dataset so
filtering never changes the reference. Failed runs are labeled, never plotted
as valid measurements. Every repetition is independently inspectable.

A native modal displays run metrics, outcomes, and complete artifact hashes.
The problem catalog exposes interfaces, arithmetic, edge cases, and reference
source links. No build step or runtime dependencies. Published JSON and scoring
code are unchanged.

Validate with the site/report unit tests, JavaScript syntax checks, and desktop
and phone browser checks for search, metric switches, matrix cells, and dialog
keyboard behavior.
