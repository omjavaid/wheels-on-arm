#!/usr/bin/env python3
"""Verify that "pure Python" packages actually install.

scan.py classifies a package as "pure" purely by looking at its wheel
FILENAME (a py3-none-any wheel exists). That is a metadata claim, not
proof. A release can still fail to install for real reasons: bad or
inconsistent metadata, a yanked release, a build backend that silently
requires a compiler despite the "any" tag, a broken dependency
constraint, etc. This script performs a REAL `pip install` of each
candidate, in isolation, and reports which ones actually work.

============================== SAFETY NOTICE ===============================
Installing a package can execute arbitrary code (setup.py / build hooks
are not sandboxed by pip). This script does not, and cannot, make that
safe. Only run it inside a disposable sandbox you are prepared to throw
away: a fresh VM, a container, or similar. Do not run it on a machine
or account that holds anything you care about.
=============================================================================

Standalone: this script does its own PyPI ranking fetch and its own
"pure" classification. It does not need scan.py or data.json to have
run first, though it will reuse docs/data.json if you point --data at
it, to save re-checking wheels that were already classified.

Usage:
  python verify_pure.py --top 500                 # scan + verify top 500
  python verify_pure.py --top 2000 --workers 8
  python verify_pure.py --data docs/data.json      # reuse an existing scan
  python verify_pure.py --top 500 --dry-run        # classify only, no installs
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

TOP_PACKAGES_URL = (
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/"
    "top-pypi-packages.min.json"
)
UA = {"User-Agent": "woa-dashboard-verify (github.com actions)"}


def fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_ranked_packages(top_n):
    data = fetch_json(TOP_PACKAGES_URL)
    rows = data["rows"][:top_n]
    return [{"rank": i, "name": r["project"], "downloads": r["download_count"]}
            for i, r in enumerate(rows, 1)]


def is_pure(pkg_name):
    """Return (is_pure, version) from the package's latest PyPI release."""
    try:
        data = fetch_json(f"https://pypi.org/pypi/{pkg_name}/json")
        version = data["info"]["version"]
        wheels = [f["filename"] for f in data["urls"] if f["filename"].endswith(".whl")]
        pure = any(w.endswith("-none-any.whl") for w in wheels)
        return pure, version
    except Exception:
        return None, None


def load_pure_candidates_from_data_json(path, top_n):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    pkgs = [p for p in d["packages"] if p["rank"] <= top_n and p["status"] == "pure"]
    return [{"rank": p["rank"], "name": p["name"], "version": p["version"]} for p in pkgs]


def find_pure_candidates(top_n, workers):
    print(f"Fetching top {top_n} package ranking...")
    ranked = get_ranked_packages(top_n)
    print(f"Classifying {len(ranked)} packages (pure vs not)...")
    candidates = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(is_pure, p["name"]): p for p in ranked}
        done = 0
        for fut in as_completed(futures):
            p = futures[fut]
            pure, version = fut.result()
            done += 1
            if done % 200 == 0:
                print(f"  classified {done}/{len(ranked)}")
            if pure:
                candidates.append({"rank": p["rank"], "name": p["name"], "version": version})
    candidates.sort(key=lambda c: c["rank"])
    return candidates


def verify_one(pkg, timeout):
    """Try a real, isolated install. Returns a result dict."""
    name, version = pkg["name"], pkg["version"]
    spec = f"{name}=={version}" if version else name
    tmp_target = tempfile.mkdtemp(prefix="woa-verify-")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--no-deps",                 # do not pull in the whole dependency tree
        "--no-cache-dir",            # every install must be genuinely fresh
        "--disable-pip-version-check",
        "--target", tmp_target,      # isolated install location, not site-packages
        spec,
    ]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        elapsed = round(time.time() - start, 1)
        ok = proc.returncode == 0
        tail = "" if ok else "\n".join(proc.stderr.strip().splitlines()[-8:])
        return {**pkg, "installed": ok, "seconds": elapsed, "error_tail": tail}
    except subprocess.TimeoutExpired:
        return {**pkg, "installed": False, "seconds": timeout,
                 "error_tail": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {**pkg, "installed": False, "seconds": round(time.time() - start, 1),
                 "error_tail": str(e)}
    finally:
        shutil.rmtree(tmp_target, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=500,
                     help="how many top-downloaded packages to consider")
    ap.add_argument("--data", type=str, default=None,
                     help="reuse an existing scan.py docs/data.json instead of "
                          "re-classifying pure/not from scratch")
    ap.add_argument("--workers", type=int, default=6,
                     help="parallel installs (keep modest; each spawns pip)")
    ap.add_argument("--timeout", type=int, default=90,
                     help="per-package install timeout, in seconds")
    ap.add_argument("--out", type=str, default="verify-results.json")
    ap.add_argument("--dry-run", action="store_true",
                     help="only find and classify candidates, do not install")
    args = ap.parse_args()

    print("=" * 70)
    print("SAFETY: this script installs real packages from PyPI, which can")
    print("run arbitrary code. Only continue if this machine is a disposable")
    print("sandbox (VM / container) you are prepared to wipe.")
    print("=" * 70)

    if args.data:
        print(f"Loading pure candidates from {args.data} ...")
        candidates = load_pure_candidates_from_data_json(args.data, args.top)
    else:
        candidates = find_pure_candidates(args.top, args.workers)

    print(f"\n{len(candidates)} pure-Python candidates in the top {args.top}.")

    if args.dry_run:
        for c in candidates:
            print(f"  {c['rank']:5d}  {c['name']}=={c['version']}")
        return

    print(f"Verifying installs ({args.workers} parallel, {args.timeout}s timeout each)...\n")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(verify_one, c, args.timeout): c for c in candidates}
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            done += 1
            mark = "OK  " if r["installed"] else "FAIL"
            print(f"[{done}/{len(candidates)}] {mark} {r['name']}=={r['version']} "
                  f"({r['seconds']}s)")
            if not r["installed"]:
                print(f"         {r['error_tail'][:200]}")

    results.sort(key=lambda r: r["rank"])
    failed = [r for r in results if not r["installed"]]

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "top_n": args.top,
        "candidates_checked": len(results),
        "failed_count": len(failed),
        "results": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")

    print("\n" + "=" * 70)
    print(f"{len(results) - len(failed)}/{len(results)} pure-tagged packages "
          f"installed successfully.")
    if failed:
        print(f"\n{len(failed)} FAILED despite being tagged pure Python "
              f"(worth a closer look):")
        for r in failed:
            print(f"  {r['rank']:5d}  {r['name']}=={r['version']}  — "
                  f"{r['error_tail'].splitlines()[-1] if r['error_tail'] else ''}")
    print(f"\nFull results written to {args.out}")


if __name__ == "__main__":
    main()
