# ADPBench site

A zero-dependency static leaderboard. Scores in `site/data/` are generated from
frozen pilot records by the harness:

```bash
adpbench site sanity --out pilot/sanity.json
adpbench site export --pilot runs/<pilot> --out site/data --sanity pilot/sanity.json
```

The results page shows one leaderboard: the newest **agent-assisted-v1** batch in
`site/data/evaluations.json`, ranked by beat-baseline rate. Earlier single-shot
and pilot-001 results are not on the site; their frozen records stay under
`pilot/results/`. Every result links to its original generation/scoring Actions job, full
workflow and evaluated code revision. Output-cap, generation, incorrect-RTL and
successful outcomes remain distinguishable.

Provider logos in `site/assets/logos/` come from
[LobeHub Icons](https://github.com/lobehub/lobe-icons) (MIT, © LobeHub) and
[Simple Icons](https://github.com/simple-icons/simple-icons) (CC0, Xiaomi).
`app.js` matches each model id (the label after its provider prefix) against a
list of 22 model families, so new models from those families get a logo
automatically; unmatched models fall back to an initial. To add a family, drop
an SVG into `site/assets/logos/` and add one pattern to `logos` in `app.js`.

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
CI execution on bot-created PRs; review the data diff first. Merging into `main`
triggers Site validation and Vercel production deployment. Publication only
opens the results PR; it does not merge it or change repository visibility.

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
`pilot/results/<batch>` and `site/data/<batch>`. Commit these
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

Deployment: Vercel project `adpbench` is connected to `arygupt/ADPBench`.
Pushes and merges to `main` automatically update
[adpbench.vercel.app](https://adpbench.vercel.app); other branches get preview
deployments. Vercel uses `site/` as its Root Directory, `.` as its Output
Directory, and empty Build and Install commands for this static site.
For a manual production deployment, run `vercel deploy --prod --project adpbench
--scope aryang20s-projects` from the repository root. The
`.github/workflows/deploy-site.yml` workflow separately validates site changes
on pull requests and pushes to `main` and uploads a downloadable site bundle.

The interface uses compact leaderboard rows with horizontal performance bars,
aligned metric columns, and a shared percentage axis. A slim top navigation leads into the rankings, with the operator matrix directly
below. The problem library lists numbered, collapsed disclosure rows in a
single column, with a download-specs button above. Clicking or using the
keyboard expands one problem at a time; direct problem links open that row.
Typography uses Inter; the palette retains `#1a1a1a` charcoal, `#e7e5e4`
off-white, `#a8a29e` stone gray, and `#f97316` orange.
Model rows rank by beat-baseline rate, correctness, or correct-run geometric
mean. The operator matrix switches between ADP ratio, cells, and cycles. On phones, tables scroll independently and model names
stay pinned while inspecting later columns.

Rate indicators use the full 0–100% scale, with details in the hover text. Geomean and
operator ratio bars show each result as a share of
the best correct result. Cells/cycles bars use best/value (lower is better).
Stars identify ties for the best value. Failed runs are labeled, never plotted
as valid measurements. Every repetition is independently inspectable.

A native modal displays run metrics, outcomes, complete artifact hashes, and
links to the original Actions jobs, workflow artifacts, and commit-pinned RTL.
The problem catalog exposes interfaces, arithmetic, edge cases, and reference
source links. No build step or runtime dependencies. Published scores and
scoring code are unchanged.

## Replay evidence

Pilot-001's six successful submissions were re-scored in Actions run
35528582943 and reproduced exactly; see
[`pilot/results/pilot-001/REPLAY.md`](../pilot/results/pilot-001/REPLAY.md).
That batch is no longer shown on the site.

Validate with the site/report unit tests, JavaScript syntax checks, and desktop
and phone browser checks for metric switches, matrix cells, and dialog keyboard
behavior.

```bash
node --test tests/test_site_evidence.mjs
python -m unittest discover -s tests -p 'test_site.py'
```
