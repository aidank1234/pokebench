#!/usr/bin/env python3
"""Fetch exact public source revisions; optionally build the macOS software core.

Never fetches a ROM, BIOS, save, emulator state, credential or model response.
Existing dirty or unexpected source checkouts are rejected, never reset/cleaned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, dry_run: bool = False) -> None:
    print("+ " + shlex.join(command), flush=True)
    if not dry_run:
        subprocess.run(command, check=True)


def git(path: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *arguments], text=True, stderr=subprocess.PIPE
    ).strip()


def local_vendor_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to((root / "vendor").resolve()):
        raise ValueError(f"Expected a path under vendor/: {relative}")
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and p.is_relative_to(root)):
        raise ValueError(f"Refusing a symlinked vendor path: {relative}")
    return path


def validate_pins(pins: dict) -> None:
    if pins.get("schema_version") != 1:
        raise ValueError("Unsupported upstream pin schema")
    records = pins["repositories"] + pins["core_dependencies"]
    for record in records:
        if not re.fullmatch(r"[0-9a-f]{40}", record["commit"]):
            raise ValueError(f"Expected a full commit hash: {record['name']}")
        if not record["url"].startswith("https://github.com/"):
            raise ValueError(f"Expected an explicit public GitHub HTTPS source: {record['name']}")
    if len({r["name"] for r in pins["core_dependencies"]}) != len(pins["core_dependencies"]):
        raise ValueError("Duplicate core dependency")
    for dependency in pins["core_dependencies"]:
        if not re.fullmatch(r"[A-Z_]+", dependency["cmake_prefix"]):
            raise ValueError("Invalid CMake dependency prefix")


def check_existing(path: Path, record: dict, *, require_pin: bool = False) -> None:
    if not path.exists():
        return
    if not (path / ".git").exists():
        raise ValueError(f"Refusing existing non-Git directory: {path}")
    if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
        raise ValueError(f"Unexpected Git checkout root: {path}")
    if git(path, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError(f"Source checkout is dirty; preserve your changes first: {path}")
    if git(path, "remote", "get-url", "origin").removesuffix(".git") != record["url"].removesuffix(".git"):
        raise ValueError(f"Source origin differs from the pinned repository: {path}")
    if require_pin and git(path, "rev-parse", "HEAD") != record["commit"]:
        raise ValueError(f"Cached dependency has a different revision; use a fresh build directory: {path}")


def fetch(path: Path, record: dict, *, dry_run: bool) -> None:
    # Dry-run always prints a fresh-checkout plan without making directories.
    if dry_run or not path.exists():
        run(["git", "init", str(path)], dry_run=dry_run)
        run(["git", "-C", str(path), "remote", "add", "origin", record["url"]], dry_run=dry_run)
    elif git(path, "rev-parse", "--verify", "HEAD") == record["commit"]:
        print(f"Already pinned: {record['name']} {record['commit']}")
        return
    run(["git", "-C", str(path), "fetch", "--depth", "1", "origin", record["commit"]], dry_run=dry_run)
    run(["git", "-C", str(path), "checkout", "--detach", record["commit"]], dry_run=dry_run)
    if not dry_run and git(path, "rev-parse", "HEAD") != record["commit"]:
        raise ValueError(f"Checkout did not resolve to the pinned commit: {path}")


def configure_command(root: Path, pins: dict) -> list[str]:
    build = pins["core_build"]
    command = ["cmake", "-S", str(local_vendor_path(root, build["source"])),
               "-B", str(local_vendor_path(root, build["directory"])),
               "-DCMAKE_BUILD_TYPE=" + build["configuration"]]
    command += [f"-D{key}={value}" for key, value in build["cmake_options"].items()]
    for dependency in pins["core_dependencies"]:
        prefix = dependency["cmake_prefix"]
        command += [f"-D{prefix}_REPOSITORY_URL={dependency['url']}",
                    f"-D{prefix}_REPOSITORY_TAG={dependency['commit']}",
                    f"-DFETCHCONTENT_SOURCE_DIR_{prefix}="]
    return command


def build_core(root: Path, pins: dict, *, jobs: int, dry_run: bool) -> None:
    if not dry_run and sys.platform != "darwin":
        raise ValueError("This optional build command targets macOS; source-only fetching is portable.")
    build = local_vendor_path(root, pins["core_build"]["directory"])
    if not dry_run:
        for dependency in pins["core_dependencies"]:
            path = local_vendor_path(root, str(build.relative_to(root) / "_deps" / (dependency["name"] + "-src")))
            check_existing(path, dependency, require_pin=True)
    run(configure_command(root, pins), dry_run=dry_run)
    if not dry_run:
        for dependency in pins["core_dependencies"]:
            path = build / "_deps" / (dependency["name"] + "-src")
            if not path.is_dir():
                raise ValueError(f"CMake did not populate expected pinned dependency: {path}")
            check_existing(path, dependency, require_pin=True)
    run(["cmake", "--build", str(build), "--config", "Release", "--parallel", str(jobs)], dry_run=dry_run)
    if not dry_run:
        output = local_vendor_path(root, pins["core_build"]["output"])
        if not output.is_file():
            raise ValueError(f"Core output not found: {output}")
        print("Built core: " + str(output))
        print("Core SHA256: " + hashlib.sha256(output.read_bytes()).hexdigest())
        print("Keep the generated build/melondsds-LICENSE.txt and upstream notices with your build.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print a fresh-checkout/build plan without running commands or using the network.")
    parser.add_argument("--build-core", action="store_true", help="Also configure and build the pinned macOS software-rendered core.")
    parser.add_argument("--jobs", type=int, default=2, help="Parallel compiler jobs (default: 2).")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    pins = json.loads((ROOT / "config/upstream-pins.json").read_text())
    validate_pins(pins)
    sources = [(local_vendor_path(ROOT, r["destination"]), r) for r in pins["repositories"] if r["fetch_with_helper"]]
    if not args.dry_run:
        # Validate every existing source before changing any source checkout.
        for path, record in sources:
            check_existing(path, record)
    for path, record in sources:
        fetch(path, record, dry_run=args.dry_run)
    if args.build_core:
        build_core(ROOT, pins, jobs=args.jobs, dry_run=args.dry_run)
    print("Harbor is installed separately from pyproject.toml; no game assets are fetched.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Source setup stopped: {error}", file=sys.stderr)
        raise SystemExit(1)
