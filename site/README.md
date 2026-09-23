# ADPBench site

A zero-dependency static leaderboard. Scores in `site/data/` are generated from
frozen pilot records by the harness:

```bash
adpbench site sanity --out pilot/sanity.json
adpbench site export --pilot runs/<pilot> --out site/data --sanity pilot/sanity.json
```

The evaluation selector keeps **OpenCode Go single-shot** results separate from
**iterative pilot-001**. The default is the new six-model Go screen. Every Go
result links to its original model-generation/scoring Actions job, full workflow
and evaluated code revision. It is never called a verified replay. Output-cap,
generation, incorrect-RTL and successful outcomes remain distinguishable.

The dated **Go compatibility checks** panel is a separately reviewed diagnostic
snapshot, outside the dataset selector and rankings. It links the successful
GLM/MiniMax request run, frozen-XOR verification, evidence report, and original
failed diagnostic. It does not turn compatibility probes into benchmark scores
or overwrite historical failures. Site tests bind its model names, output-token
counts and verification claims to the committed diagnostic receipt and RTL
hashes. Updating the panel launches no model requests.

## Automatic results PRs

The **Publish validated model results** workflow follows completed, eligible
**OpenCode Go model runs** on `main`, including failed or interrupted evaluations:

`completed model run → artifact validation → results PR → human merge → Site build`

Preparation-only, skipped/duplicate-claim, fork and PR runs cannot publish.
`pilot/publication-policy.json` registers the supported workflow and reviewed
plan. A new batch needs a new authorized plan and a reviewed policy update;
this automation neither launches models nor grants spending authorization.

The publisher checks the original repository, run attempt, ancestral main commit,
unchanged problem/flow definitions, plan, budgets, generation settings, scorer
records and frozen RTL hashes. It downloads size-bounded artifacts, verifies
GitHub's SHA-256 digests, and rejects unsafe ZIP paths, links, duplicate JSON
keys and nonfinite numbers. It executes only trusted main-branch publisher code,
never artifact RTL or downloaded scripts. No Go credentials are passed to it.

The publisher reads the literal `PLAN` selected by the original workflow revision,
not the current default. This keeps older 8,192-token runs reproducible after
switching to the separately named provider-maximum configuration. Generation
records must match the specific model's reviewed limit. The browser shows the
budget range for provider-maximum datasets and the exact model limit in details;
it never presents a finite provider response as unlimited.

The PR contains the frozen records, site snapshot, dataset catalog entry and a
hash receipt in `pilot/publications/`. Existing snapshots are immutable: changed
scores or sources stop publication. Repeating publication reuses an existing
PR or produces no change, and never force-pushes or reopens a closed PR. The
site's validated `site/data/evaluations.json` catalog makes new datasets available
without editing the browser code. CI verifies receipt hashes against the frozen
files and checks that the website shows the same outcomes.

The repository's Actions PR-creation setting must be enabled. Default token
permissions stay read-only; only the publisher job has contents/PR write access.
It **never approves or merges PRs**. GitHub may require a maintainer to approve
CI execution on bot-created PRs; review the data diff first. A human merge
triggers Site validation and builds the updated downloadable site bundle.
Public hosting remains separately gated by `ENABLE_PAGES`; publication does
not enable Pages or change repository visibility.

Backfill an already-completed run without any model calls:

```bash
gh workflow run publish-results.yml -f run_id=35671789622
# Validation only (no branch, commit or PR):
gh workflow run publish-results.yml -f run_id=35671789622 -f validate_only=true
```

For a local validation/staging pass, run
`python -m scripts.publish_results --run-id RUN_ID`. It downloads and stages
data files but does not commit or push unless `--open-pr` is supplied; that flag
requires a clean checkout. Keep this separate from active development edits.

The lower-level manual exporter remains available for debugging. Download
`go-core-*` artifacts preserving artifact-name directories, then run:

```bash
gh run download RUN_ID --pattern 'go-core-*' --dir /path/to/artifacts
python -m scripts.publish_go --run-id RUN_ID --artifacts /path/to/artifacts
```

The publisher checks the reviewed plan, model/problem identities, original
Actions run/attempt/commit, scorer manifest and frozen RTL hashes before writing
anything. It rejects skipped jobs, missing records in successful jobs, altered RTL,
and overwrites. If an interrupted job saved generation but no final scoring
record, it preserves that generation and frozen RTL as `github-generation-only`:
score unknown, never a claimed completed evaluation.
For the current reviewed plan it emits all 12 outcomes, not only successful submissions, under
`pilot/results/go-core-20260922` and `site/data/go-core-20260922`. Commit these
reviewed records and the generated site data to publish a new frozen snapshot.
Provider raw responses stay in the private Actions artifacts, not the website.
If a job fails or times out before uploading its artifact, its two scheduled slots are
explicitly labeled **job-status-only**: scores, RTL and usage are unknown, not
fabricated. The token total is then a lower bound. Subsequent workflows preserve
generation evidence before starting expensive scoring.

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

The automatic publisher uses IDs from actual model jobs and publishes their
provenance alongside the frozen records. It does not claim to replay scores;
verified replay evidence still requires an actual score comparison and review.
Do not infer correctness from a workflow's green badge alone. No GitHub token
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
