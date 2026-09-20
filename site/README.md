# ADPBench site

A zero-dependency static leaderboard. Scores in `site/data/` are generated from
frozen pilot records by the harness:

```bash
adpbench site sanity --out pilot/sanity.json
adpbench site export --pilot runs/<pilot> --out site/data --sanity pilot/sanity.json
```

Preview locally:

```bash
python3 -m http.server 8000 --directory site
```

Deployment: `.github/workflows/deploy-site.yml` validates site changes on pull
requests and pushes to `main`. After validation, it uploads the site as a
downloadable workflow artifact. On `main` it configures and deploys to
GitHub Pages when Pages is enabled and the `ENABLE_PAGES=true` repository
variable is set. GitHub Pages is free for public repositories; a private
repository needs a paid plan, in which case deploy the artifact to any static
host instead.

The interface uses compact leaderboard rows with horizontal performance bars,
aligned metric columns, and a shared percentage axis. A slim top navigation and
experiment summary lead into the rankings, with the operator matrix directly
below. The problem library lists numbered, collapsed disclosure rows in a
single column, with a download-specs button above. Clicking or using the
keyboard expands one problem at a time; direct problem links open that row.
Typography uses Inter; the palette retains `#1a1a1a` charcoal, `#e7e5e4`
off-white, `#a8a29e` stone gray, and `#f97316` orange.
Model rows rank by beat-baseline rate, correctness, or correct-run geometric
mean. The operator matrix switches between ADP ratio, cells, and cycles. Search
filters both tables. On phones, tables scroll independently and model names
stay pinned while inspecting later columns.

Rate indicators use the full 0–100% scale; 95% Wilson confidence bounds appear
as whiskers and numeric ranges, with details in the hover text. Geomean and
operator ratio bars show each result as a share of
the best correct result. Cells/cycles bars use best/value (lower is better).
Stars identify ties for the best value, computed across the full dataset so
filtering never changes the reference. Failed runs are labeled, never plotted
as valid measurements. Every repetition is independently inspectable.

A native modal displays run metrics, outcomes, complete artifact hashes, and
links to verified GitHub replay jobs, workflow artifacts, and commit-pinned RTL.
The problem catalog exposes interfaces, arithmetic, edge cases, and reference
source links. No build step or runtime dependencies. Published scores and
scoring code are unchanged.

## Replay evidence

`site/data/evidence.json` is a separately reviewed provenance index, not a
leaderboard input. The score exporter does not overwrite it. Its version-1
schema identifies the pilot, then each run by model label, problem, attempt,
and submission SHA-256. The `recorded` fields bind the evidence to the exact
correctness, cells, cycles, and ratio shown on the leaderboard. Each `replay`
stores the repository, workflow run ID, run attempt, job ID, evaluated commit,
frozen submission path, completion time, and comparison outcome (`match`).

The browser only displays verification when all identities and metrics match,
the comparison succeeded, and link metadata is valid. Evidence for another
pilot, different RTL, or different metrics cannot verify a result. Missing or
unavailable evidence does not affect scores. A replay never adds a model attempt.
The six pilot-001 entries reference successful score comparisons in Actions run
35528582943; they are re-evaluations, not original model-generation jobs. A green
preparation-only workflow is not replay or model-run evidence.

When publishing future results, collect IDs from the actual jobs and publish
their provenance alongside the frozen records. Do not infer success from the
workflow's green badge alone. Keep evidence updates reviewed until automatic
publication validates submission hashes and score comparisons. No GitHub token
or live API polling is needed by the website. GitHub logs/artifacts have limited
retention, so source links are pinned to the evaluated commit. The replay index
survives score re-exports; a new pilot needs its own matching evidence index.

Validate with the site/report unit tests, JavaScript syntax checks, and desktop
and phone browser checks for search, metric switches, matrix cells, and dialog
keyboard behavior.

```bash
node --test tests/test_site_evidence.mjs
python -m unittest discover -s tests -p 'test_site.py'
```
