#!/usr/bin/env python3
"""Fetch the CVA6 RTL subset needed for the GPCP SoC.

github.com:443 is blocked in this environment, but raw.githubusercontent.com and
api.github.com are reachable, so we reconstruct the working tree from the
GitHub tree API + raw file downloads.

Usage:
    python3 scripts/fetch_cva6.py [--dest third_party/cva6]
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAIN_REPO = "openhwfoundation/cva6"
MAIN_REF = "master"

# directories we need for a minimal SoC + AXI crossbar + test harness
PREFIXES = ("core/", "corev_apu/", "vendor/", "config/", "common/")
SKIP_DIRS = (
    "corev_apu/fpga/",
    "corev_apu/openpiton/",
    "corev_apu/altera/",
)
TOP_FILES = ("Bender.yml", "src_files.yml", "Flist.ariane", "verilator_config.vlt",
             "ariane.core", "Makefile", "LICENSE", ".gitmodules")

# submodule path -> (repo, sha) as recorded in the parent tree
SUBMODULES = {
    "core/cvfpu": ("openhwgroup/cvfpu", None),
    "core/cache_subsystem/hpdcache": ("openhwgroup/cv-hpdcache", None),
    "corev_apu/register_interface": ("pulp-platform/register_interface", None),
    "corev_apu/axi_mem_if": ("pulp-platform/axi_mem_if", None),
    "corev_apu/rv_plic": ("pulp-platform/rv_plic", None),
    "corev_apu/riscv-dbg": ("pulp-platform/riscv-dbg", None),
    "corev_apu/tb/common_verification": ("pulp-platform/common_verification", None),
}


def get(url: str, binary: bool = False, retries: int = 3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gpcp-fetch"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read() if binary else r.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def tree(repo: str, ref: str):
    data = json.loads(get(f"{API}/repos/{repo}/git/trees/{ref}?recursive=1"))
    if "tree" not in data:
        raise SystemExit(f"cannot list tree for {repo}@{ref}: {data.get('message')}")
    return data["tree"]


def wanted(path: str) -> bool:
    if path in TOP_FILES:
        return True
    if not any(path.startswith(p) for p in PREFIXES):
        return False
    if any(path.startswith(p) for p in SKIP_DIRS):
        return False
    return True


def fetch_files(dest: Path, repo: str, ref: str, entries, label: str, all_files: bool = False):
    files = [e for e in entries if e["type"] == "blob" and (all_files or wanted(e["path"]))]
    print(f"[{label}] {len(files)} files from {repo}@{ref[:8]}")
    errors = []

    def one(e):
        p = dest / e["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = get(f"{RAW}/{repo}/{ref}/{e['path']}", binary=True)
            p.write_bytes(data)
            return len(data), None
        except Exception as ex:
            return 0, (e["path"], str(ex))

    total = 0
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        for size, err in pool.map(one, files):
            total += size
            if err:
                errors.append(err)
    print(f"[{label}] {total/1e6:.1f} MB written, {len(errors)} errors")
    for p, msg in errors[:5]:
        print("   !", p, msg)
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="third_party/cva6")
    ap.add_argument("--only-submodules", action="store_true")
    args = ap.parse_args()
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    entries = tree(MAIN_REPO, MAIN_REF)
    # fill in submodule SHAs from the parent tree
    for e in entries:
        if e["type"] == "commit" and e["path"] in SUBMODULES:
            repo, _ = SUBMODULES[e["path"]]
            SUBMODULES[e["path"]] = (repo, e["sha"])

    errs = []
    if not args.only_submodules:
        errs = fetch_files(dest, MAIN_REPO, MAIN_REF, entries, "cva6")
    for path, (repo, sha) in SUBMODULES.items():
        sub = dest / path
        sub.mkdir(parents=True, exist_ok=True)
        files = tree(repo, sha)
        errs += fetch_files(sub, repo, sha, files, path, all_files=True)

    (dest / ".fetched.json").write_text(json.dumps(
        {"repo": MAIN_REPO, "ref": MAIN_REF, "submodules": SUBMODULES}, indent=2))
    print("done:", len(errs), "errors")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
