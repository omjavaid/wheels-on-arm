#!/usr/bin/env python3
"""Print a ranked shortlist of which "missing & not tracked" packages to
chase first.

This is a convenience wrapper only — every number it prints already
lives in docs/data.json after a scan.py run. It makes no network calls
and adds no new data; it just sorts and formats what's already there,
for a quick "what should I test next" glance from the terminal.

The ranking itself (the "priority" field) is computed in scan.py by
compute_priority() — read that function's docstring for the exact
formula and reasoning. Short version: usage (downloads) is the dominant
factor, adjusted up or down by how easy the fix looks (already builds
for Arm64 elsewhere, uses the stable ABI, has a mature release pipeline,
recent releases) or looks hard (old, likely-unmaintained project).

IMPORTANT CAVEAT — read before trusting a score blindly:
The score is a heuristic from wheel filenames and release metadata. It
cannot see real blockers such as a missing native dependency (e.g. the
PostgreSQL libpq library has no Windows Arm64 build yet, which blocks
every package that links it, whatever their own wheel history says).
It also cannot see cross-package aliases: a package can look "not
tracked" simply because the community tracker recorded a differently
named sibling package instead (psycopg2-binary vs. psycopg-binary is a
real example this tool will surface). Always do a quick recon check —
search the package's own repo issues, and the tracker, for its exact
name and any close relatives — before spending build time on a
high-scoring candidate.

Usage:
  python prioritize.py                    # top 20 from docs/data.json
  python prioritize.py -n 50
  python prioritize.py --data other.json
  python prioritize.py --min-downloads 5000000
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20, help="how many to show")
    ap.add_argument("--data", type=str, default=str(ROOT / "docs" / "data.json"))
    ap.add_argument("--min-downloads", type=int, default=0,
                     help="skip packages below this monthly download count")
    ap.add_argument("--strategic-only", action="store_true",
                     help="show only packages on the strategic AI/ML/data-science list")
    args = ap.parse_args()

    path = Path(args.data)
    if not path.exists():
        raise SystemExit(f"{path} not found — run scan.py first.")

    d = json.loads(path.read_text(encoding="utf-8"))
    candidates = [
        p for p in d["packages"]
        if p["status"] == "missing" and not p["covered"]
        and p["downloads"] >= args.min_downloads
        and (not args.strategic_only or p.get("strategic"))
    ]
    candidates.sort(key=lambda p: -(p["priority"] or 0))

    print(f"Scan: {d['generated']} · top {d['top_n']} by downloads "
          f"({d['total_scanned']} scanned total) · "
          f"{len(candidates)} missing & not tracked\n")
    print(f"{'Priority':>8}  {'Downloads/mo':>13}  {'':1} {'Package':<24} "
          f"{'ABI3':>5} {'Arm64 mac/linux':>16} {'Wheels':>6} {'Days since rel.':>15}")
    print("-" * 105)
    for p in candidates[:args.n]:
        dl = p["downloads"]
        dl_s = f"{dl/1e9:.2f}B" if dl >= 1e9 else f"{dl/1e6:.0f}M" if dl >= 1e6 else f"{dl/1e3:.0f}K"
        star = "*" if p.get("strategic") else " "
        arm = ("mac+linux" if p["has_macos_arm64"] and p["has_linux_arm64"]
               else "mac" if p["has_macos_arm64"]
               else "linux" if p["has_linux_arm64"] else "no")
        print(f"{p['priority']:>8.2f}  {dl_s:>13}  {star:1} {p['name']:<24} "
              f"{str(p['abi3']):>5} {arm:>16} "
              f"{str(p['n_wheels']):>6} {str(p['days_since_release']):>15}")
        if p.get("repo_url"):
            print(f"           {p['repo_url']}")

    print("\n* = on the strategic AI/ML/data-science list (strategic.json)")
    print("\nReminder: this is a heuristic, not a verdict. A high score means")
    print("'worth a recon check first' — it does not mean 'guaranteed easy'.")
    print("Known blind spots: missing native dependencies (e.g. libpq on")
    print("Windows Arm64), and packages tracked upstream under a different")
    print("but related name. Check the package's own repo and the tracker")
    print("issue by name before committing build time.")


if __name__ == "__main__":
    main()
