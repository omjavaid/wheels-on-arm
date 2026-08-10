# WoA Python Wheel Dashboard

A simple dashboard: for the top N most-downloaded PyPI packages, shows
whether each has a native **Windows on Arm (`win_arm64`)** wheel, and
whether it's already tracked at
[khmyznikov/PyEnv-WoA-State#1](https://github.com/khmyznikov/PyEnv-WoA-State/issues/1).

## How it works

```
top-pypi-packages ranking ──► scan.py ──► docs/data.json ──► docs/index.html
                                 ▲
                         config.json (how many packages)
                         covered.json (tracked-elsewhere list)
                         strategic.json (important regardless of downloads)
```

- **`config.json`** — one number, `top_n`. This is how many top-downloaded
  packages get scanned. Set it to 1000, 2000, 3000, or anything else.
- **`scan.py`** — fetches the ranked package list, checks each package's
  latest PyPI release for a `win_arm64` wheel, marks it "covered" if its
  name appears in `covered.json`, marks it "strategic" if it appears in
  `strategic.json`, and computes a priority score for real gaps (see
  below).
- **`covered.json`** — a plain list of package names already tracked in
  the community issue. Seeded from the issue on 2026-07-09. Update by
  hand when that issue changes (see below).
- **`strategic.json`** — a plain list of AI/ML/data-science package names
  considered important regardless of raw download count. These are always
  scanned and always shown, even outside `top_n` (see below).
- **`docs/index.html`** — the dashboard. A dropdown lets you view the top
  1000 / 2000 / 3000 / all of whatever was scanned, without re-scanning.

Every package lands in exactly one status: **native** (has a
`win_arm64` wheel), **pure** (universal wheel, none needed), **missing**
(the real gap — has other platform wheels, including Windows x64, but
not Arm64), **no-windows** (wheels exist for other platforms but none
for Windows at all), or **sdist-only** (no wheels published for any
platform — a bigger ask than adding one target, so kept separate from
"missing" rather than inflating that count).

## Deploy (one time)

1. Push this repo to GitHub.
2. Settings → Pages → Source: Deploy from a branch → Branch: `main`,
   folder: `/docs`.
3. Settings → Actions → General → Workflow permissions → **Read and
   write permissions** (so the weekly scan can commit its results).

The site appears at `https://<you>.github.io/<repo>/` within a minute or two.

## Changing how many packages are covered

Edit `config.json`:

```json
{ "top_n": 3000 }
```

Commit and push — the push itself triggers a re-scan (see the workflow's
`paths` trigger), so `docs/data.json` updates automatically. You can also
do a one-off run without changing the file: **Actions → Update dashboard
→ Run workflow → top: 3000**.

## Updating the "tracked at #1" list

The community tracker changes over time. To refresh `covered.json`:
open the tracker issue, copy the package names from both its tables,
run them through a simple lowercase + dedupe, and replace the file's
contents (a plain JSON array of names). Commit — the push retriggers a
scan automatically.

## Verifying "pure Python" is real

`scan.py` classifies a package as "pure" from its wheel *filename* only.
That's a claim, not proof — a release can still be broken. A separate
script does a real, isolated install of each pure-tagged package to check:

```bash
python verify_pure.py --top 500
```

**Run this only inside a disposable sandbox (a throwaway VM or
container) — it installs real packages from PyPI, which can execute
arbitrary code during install, same as any `pip install` ever does.**
Each package is installed with `--no-deps --target <isolated temp dir>`,
never into your real environment, and the temp dir is deleted right
after each check regardless of success or failure.

```bash
python verify_pure.py --top 500              # fetch + classify + verify
python verify_pure.py --data docs/data.json --top 500   # reuse an existing scan
python verify_pure.py --top 500 --dry-run     # just list candidates, install nothing
python verify_pure.py --top 2000 --workers 8 --timeout 120
```

Results are written to `verify-results.json` and any package tagged
"pure" that still failed to install is called out explicitly at the end
of the run — that's the signal worth investigating.



## Which "missing & not tracked" package matters most?

There can be dozens of these. A simple download-count sort isn't enough
— a package with huge downloads but a genuinely hard blocker (say, a
missing native library) isn't a good next target, while a smaller
package that already builds for Arm64 on Mac and Linux is often a
one-line CI fix. `scan.py` computes a **priority score** for every
"missing" package, from signals already in the same PyPI response it
fetches (no extra network cost):

- **Downloads** (usage scale — the dominant factor; more users helped)
- **+ already ships an Arm64 wheel for macOS and/or Linux** (shown as
  its own "Arm64 elsewhere" column — mac, linux, mac+linux, or no) —
  strong evidence the code already works on Arm64 somewhere, so
  Windows is probably a missing CI entry, not new porting work
- **+ uses the stable ABI (abi3)** — one wheel would cover every future
  Python version
- **+ publishes 8+ wheel variants today** — a mature, automated release
  pipeline, so adding one more target is usually a config edit
- **− no release in the last 1-2 years** — may be unmaintained; even a
  perfect PR might never ship
- **+ flat bonus for "strategic" packages** — see below

The exact formula and reasoning are documented in `compute_priority()`
in `scan.py`. The dashboard sorts by this by default; hover a Priority
cell to see which signals drove that package's score.

**This is a heuristic, not a verdict.** It can't see a missing native
dependency (e.g. PostgreSQL's libpq has no Windows Arm64 build, which
blocks every package linking it regardless of that package's own wheel
history), and it can't detect that a package is tracked upstream under
a different but related name (`psycopg2-binary` scores high here
precisely because the tracker only lists `psycopg-binary`, a different
PyPI package with the same root blocker). Always do a quick recon check
before spending build time on a high-scoring candidate.

### Important, but not necessarily high-download: strategic.json

Downloads alone under-rate packages that matter to modern AI/ML/data
workloads before their download counts catch up — a brand-new library
everyone in that world is adopting can matter more than its current
rank suggests. `strategic.json` is a hand-curated list of such package
names (torch, transformers, langchain, vllm, chromadb, ray, polars, and
similar). Any package on this list:

- is **always scanned**, even if its download rank falls outside
  whatever `top_n` you've configured — a strategic package with a low
  rank isn't silently skipped
- is **always shown** on the dashboard regardless of the "Top N" view
  selector, marked with a ★ next to its name
- gets a **flat +2.0 bonus** to its priority score (roughly equivalent
  to a 100x jump in downloads on the log scale) — enough to surface it
  above much bigger packages when its feasibility signals are also
  good, without letting it completely dominate the list

Update it the same way as `covered.json`: edit the plain JSON array,
commit, push — the push retriggers a scan automatically.

For a quick terminal shortlist without opening the dashboard:

```bash
python prioritize.py              # top 20, from an existing docs/data.json
python prioritize.py -n 50
python prioritize.py --min-downloads 5000000
python prioritize.py --strategic-only     # only the AI/ML/data-science list
```

## Running locally

```bash
python scan.py                 # uses top_n from config.json
python scan.py --top 500       # quick test run
python -m http.server -d docs  # preview at http://localhost:8000
```
