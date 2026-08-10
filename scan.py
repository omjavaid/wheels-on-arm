#!/usr/bin/env python3
"""WoA wheel dashboard scanner.

For the top N PyPI packages (N set in config.json):
  - checks whether the latest release has a native win_arm64 wheel
  - marks whether the package is already tracked at
    github.com/khmyznikov/PyEnv-WoA-State/issues/1 (covered.json)

Writes docs/data.json, consumed by docs/index.html.

Usage:
  python scan.py                 # uses top_n from config.json
  python scan.py --top 1000      # override for one run
"""
import argparse
import concurrent.futures
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
TOP_PACKAGES_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/"
    "top-pypi-packages.min.json"
)
UA = {"User-Agent": "woa-dashboard (github.com actions)"}


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def normalize_forge_url(url, forges):
    """Trim a forge URL down to its repo root (owner/repo), dropping
    trailing paths like /issues, /blob/main/CHANGELOG.rst, /wiki, etc.
    Leaves anything we don't recognize untouched.
    """
    low = url.lower()
    for forge in forges:
        marker = forge + "/"
        idx = low.find(marker)
        if idx == -1:
            continue
        after = url[idx + len(marker):].split("/")
        if len(after) >= 2 and after[0] and after[1]:
            scheme_host = url[: idx + len(marker)]
            repo = after[1]
            if repo.endswith(".git"):
                repo = repo[:-4]
            return f"{scheme_host}{after[0]}/{repo}"
    return url


def pick_repo_url(info):
    """Pick the best 'work on the release here' link from PyPI project
    metadata: prefer an actual source repo (GitHub/GitLab/Codeberg/etc.)
    over a generic homepage, since that's where issues/PRs get filed.
    """
    urls = {}
    if info.get("project_urls"):
        urls.update(info["project_urls"])
    if info.get("home_page"):
        urls.setdefault("Homepage", info["home_page"])

    forges = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org",
              "sourceforge.net", "sr.ht")

    # 1) a label that clearly names the source repo, checked in priority
    #    order so we don't grab an "Issues" or "Changelog" link instead
    priority_labels = ("source code", "source", "repository", "code",
                        "github", "homepage", "home")
    lower_urls = {k.lower(): v for k, v in urls.items()}
    for label in priority_labels:
        if lower_urls.get(label):
            return normalize_forge_url(lower_urls[label], forges)

    # 2) no labeled match — scan all values for any known code forge link
    for value in urls.values():
        if value and any(f in value.lower() for f in forges):
            return normalize_forge_url(value, forges)

    # 3) fall back to a homepage, if it isn't just pypi.org linking to itself
    home = urls.get("Homepage")
    if home and "pypi.org" not in home.lower():
        return home

    return None


STRATEGIC_BONUS = 2.0  # see compute_priority() docstring for reasoning


def compute_priority(downloads, abi3, has_macos_arm64, has_linux_arm64,
                      n_wheels, days_since_release, strategic):
    """Score how worth chasing a missing-wheel package is.

    This is deliberately simple and fully transparent — every input is a
    number already visible in the dashboard, so anyone can recompute a
    package's score by hand and see why it landed where it did.

    Base: log10(downloads) — usage scale is the dominant factor, since
    the whole point is fixing installs for as many people as possible.
    Going from 1M to 10M downloads/month adds 1.0 to the base score;
    going from 10M to 100M adds another 1.0. This keeps a handful of
    giant packages from swamping everything else while still clearly
    separating "111M downloads" from "1M downloads".

    Then a feasibility multiplier adjusts for how easy the fix likely
    is, based on signals already visible in the package's own PyPI
    metadata:

      +0.4  already ships a wheel for Arm64 macOS OR Arm64 Linux
            (aarch64). The strongest signal there is: the codebase has
            already been ported to Arm64 somewhere. Windows is very
            likely just a missing CI entry, not new porting work.
      +0.2  uses the stable ABI (abi3). One Windows Arm64 wheel would
            cover every future Python version — a small one-time fix
            with a permanent payoff.
      +0.1  publishes 8 or more wheel variants today. That's evidence
            of a mature, automated multi-platform release pipeline —
            adding one more target is usually a config-file edit, not
            new infrastructure.
      -0.5  latest release is more than 2 years old. The project may be
            unmaintained; even a perfect pull request might never get
            merged or released.
      -0.25 latest release is 1-2 years old. Slower-moving; some risk
            the same way, at half weight.

    The multiplier is clamped to a minimum of 0.15 so no package's
    score goes to zero or negative — an old, hard-looking package with
    huge downloads can still surface if nothing easier is available.

    Finally, a flat STRATEGIC_BONUS (+2.0, roughly equivalent to a 100x
    downloads jump on the log scale) is added for packages on the
    manually curated strategic.json list: modern AI/ML/data-science
    tooling that matters to what people are building right now, even
    when its raw download count doesn't yet reflect that. This is
    additive, not multiplied, so it can lift a smaller-but-important
    package into view without letting it completely dominate the list
    the way a multiplier would.
    """
    import math
    base = math.log10(max(downloads, 1))

    mult = 1.0
    if has_macos_arm64 or has_linux_arm64:
        mult += 0.4
    if abi3:
        mult += 0.2
    if n_wheels is not None and n_wheels >= 8:
        mult += 0.1
    if days_since_release is not None:
        if days_since_release > 730:
            mult -= 0.5
        elif days_since_release > 365:
            mult -= 0.25
    mult = max(mult, 0.15)

    score = base * mult
    if strategic:
        score += STRATEGIC_BONUS
    return round(score, 3)


def check_wheel_status(pkg_name):
    """Return a dict describing a package's newest PyPI release:

      status    - "native" (ships win_arm64), "pure" (universal wheel,
                  none needed), "missing" (the real gap: has other
                  platform wheels, including Windows x64, but not
                  win_arm64), "no-windows" (has wheels for other
                  platforms but none for Windows, any arch), "sdist-only"
                  (no wheels published at all, for any platform — a
                  bigger ask than adding one target), or "unknown"
                  (PyPI could not be reached)
      version   - latest release version string
      repo_url  - the project's source repository, when PyPI's metadata
                  has one (see pick_repo_url) — where to file an issue/PR
      has_win_amd64, has_win32
                - whether a Windows x64 / x86 wheel exists today. Shown
                  as its own column so "does it install on ordinary
                  Windows at all" is visible at a glance, separate from
                  the win_arm64 status.
      abi3, has_macos_arm64, has_linux_arm64, n_wheels,
      days_since_release
                - signals used to score how worth chasing a "missing"
                  package is; see compute_priority() for the formula.
                  Computed for every package for consistency, but only
                  meaningful to look at for status == "missing".

    All of this comes from ONE PyPI API call per package — no extra
    network cost for the priority signals.
    """
    for attempt in range(3):
        try:
            data = fetch_json(f"https://pypi.org/pypi/{pkg_name}/json")
            info = data["info"]
            version = info["version"]
            files = data["urls"]
            wheels = [f["filename"] for f in files if f["filename"].endswith(".whl")]
            repo_url = pick_repo_url(info)

            abi3 = any("-abi3-" in w for w in wheels)
            has_macos_arm64 = any("macosx" in w and "arm64" in w for w in wheels)
            has_linux_arm64 = any(
                ("manylinux" in w or "musllinux" in w) and "aarch64" in w
                for w in wheels
            )
            has_win_amd64 = any("win_amd64" in w for w in wheels)
            has_win32 = any("win32" in w for w in wheels)
            n_wheels = len(wheels)
            days_since_release = None
            upload_times = [f.get("upload_time_iso_8601") for f in files if f.get("upload_time_iso_8601")]
            if upload_times:
                latest_upload = max(upload_times)
                try:
                    dt = datetime.fromisoformat(latest_upload.replace("Z", "+00:00"))
                    days_since_release = (datetime.now(timezone.utc) - dt).days
                except ValueError:
                    pass

            if not wheels:
                # no wheels published for ANY platform — the project
                # hasn't adopted wheel-building at all yet. A much
                # bigger ask than "add one build target", so this is
                # tracked separately and never priority-scored as a
                # normal win_arm64 gap.
                status = "sdist-only"
            elif any("win_arm64" in w for w in wheels):
                status = "native"
            elif any(w.endswith("-none-any.whl") for w in wheels):
                status = "pure"
            elif not (has_win_amd64 or has_win32):
                # ships platform wheels, but none for Windows at all (any
                # arch) — a bigger ask than "add one build target", so
                # it's tracked separately too
                status = "no-windows"
            else:
                status = "missing"

            return {
                "status": status, "version": version, "repo_url": repo_url,
                "abi3": abi3, "has_macos_arm64": has_macos_arm64,
                "has_linux_arm64": has_linux_arm64,
                "has_win_amd64": has_win_amd64, "has_win32": has_win32,
                "n_wheels": n_wheels, "days_since_release": days_since_release,
            }
        except Exception:
            time.sleep(1 + attempt)
    return {
        "status": "unknown", "version": None, "repo_url": None,
        "abi3": False, "has_macos_arm64": False, "has_linux_arm64": False,
        "has_win_amd64": False, "has_win32": False,
        "n_wheels": None, "days_since_release": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=None,
                     help="override top_n from config.json")
    ap.add_argument("--workers", type=int, default=20)
    args = ap.parse_args()

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    top_n = args.top or config["top_n"]

    covered = set(json.loads((ROOT / "covered.json").read_text(encoding="utf-8")))
    strategic = set(n.lower() for n in json.loads((ROOT / "strategic.json").read_text(encoding="utf-8")))

    print(f"Fetching top {top_n} package list...")
    ranking = fetch_json(TOP_PACKAGES_URL)
    rows = ranking["rows"][:top_n]

    # Guarantee every strategic (AI/ML/data-science) package is scanned,
    # even if its download rank falls outside top_n — that's the whole
    # point of the strategic list: importance isn't only about volume.
    present = {r["project"].lower() for r in rows}
    full_by_name = {r["project"].lower(): r for r in ranking["rows"]}
    added_from_strategic = []
    for name in sorted(strategic):
        if name in present:
            continue
        if name in full_by_name:
            rows.append(full_by_name[name])
            added_from_strategic.append(name)
        else:
            # not in the ranking source at all (very new/niche) — still
            # scan it, with an honest zero download count rather than
            # silently dropping it
            rows.append({"project": name, "download_count": 0})
            added_from_strategic.append(name + " (no ranking data)")
    if added_from_strategic:
        print(f"Adding {len(added_from_strategic)} strategic packages outside "
              f"top {top_n}: {', '.join(added_from_strategic)}")

    print(f"Checking {len(rows)} packages against PyPI ({args.workers} workers)...")
    results = []

    def check(row):
        name = row["project"]
        r = check_wheel_status(name)
        is_strategic = name.lower() in strategic
        priority = None
        if r["status"] == "missing":
            priority = compute_priority(
                downloads=row["download_count"],
                abi3=r["abi3"], has_macos_arm64=r["has_macos_arm64"],
                has_linux_arm64=r["has_linux_arm64"], n_wheels=r["n_wheels"],
                days_since_release=r["days_since_release"], strategic=is_strategic,
            )
        return {
            "rank": None,  # filled after sort below
            "name": name,
            "downloads": row["download_count"],
            "version": r["version"],
            "status": r["status"],
            "covered": name.lower() in covered,
            "strategic": is_strategic,
            "repo_url": r["repo_url"],
            "abi3": r["abi3"],
            "has_macos_arm64": r["has_macos_arm64"],
            "has_linux_arm64": r["has_linux_arm64"],
            "has_win_amd64": r["has_win_amd64"],
            "has_win32": r["has_win32"],
            "n_wheels": r["n_wheels"],
            "days_since_release": r["days_since_release"],
            "priority": priority,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, res in enumerate(ex.map(check, rows), 1):
            results.append(res)
            if i % 200 == 0:
                print(f"  {i}/{len(rows)}")

    # Assign each package its TRUE global download rank (its position in
    # the full ranking source), not just its position in our scan batch.
    # For the normal top_n slice these are identical; it only matters for
    # strategic packages appended above, whose real rank may sit well
    # outside top_n — 999999 marks a package absent from the ranking
    # source entirely (extremely new or obscure).
    global_rank = {r["project"].lower(): i + 1 for i, r in enumerate(ranking["rows"])}
    for r in results:
        r["rank"] = global_rank.get(r["name"].lower(), 999999)
    results.sort(key=lambda r: r["rank"])

    n_native = sum(1 for r in results if r["status"] == "native")
    n_pure = sum(1 for r in results if r["status"] == "pure")
    n_missing = sum(1 for r in results if r["status"] == "missing")
    n_no_windows = sum(1 for r in results if r["status"] == "no-windows")
    n_sdist_only = sum(1 for r in results if r["status"] == "sdist-only")
    n_unknown = sum(1 for r in results if r["status"] == "unknown")
    n_gap_uncovered = sum(
        1 for r in results if r["status"] == "missing" and not r["covered"]
    )
    n_strategic = sum(1 for r in results if r["strategic"])
    n_strategic_gap = sum(
        1 for r in results if r["strategic"] and r["status"] == "missing"
    )

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "top_n": top_n,
        "total_scanned": len(results),
        "source_rank_date": ranking["last_update"],
        "covered_list_size": len(covered),
        "strategic_list_size": len(strategic),
        "summary": {
            "native": n_native,
            "pure": n_pure,
            "missing": n_missing,
            "no_windows": n_no_windows,
            "sdist_only": n_sdist_only,
            "unknown": n_unknown,
            "gap_not_covered": n_gap_uncovered,
            "strategic_scanned": n_strategic,
            "strategic_missing": n_strategic_gap,
        },
        "packages": results,
    }
    out_path = ROOT / "docs" / "data.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {out_path}")
    print(f"summary: {out['summary']}")


if __name__ == "__main__":
    main()
