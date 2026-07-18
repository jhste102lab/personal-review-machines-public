#!/usr/bin/env python3
"""Remove inactive personal-review workspaces after a retention period."""

from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--older-than-days", type=float, default=7.0)
    parser.add_argument(
        "--all-inactive",
        action="store_true",
        help="ignore the retention period; use only for a reviewed one-off cleanup",
    )
    parser.add_argument("--apply", action="store_true", help="delete candidates instead of printing them")
    return parser.parse_args()


def active_workspace_paths(work_dir: Path) -> set[Path]:
    """Return workspace paths currently used as a process cwd or open file."""
    active: set[Path] = set()
    proc_dir = Path("/proc")
    work_dir_text = f"{work_dir}{os.sep}"

    for process_dir in proc_dir.iterdir():
        if not process_dir.name.isdigit():
            continue
        for path in (process_dir / "cwd", *(process_dir / "fd").glob("*")):
            try:
                target = os.readlink(path)
            except OSError:
                continue
            if not target.startswith(work_dir_text):
                continue
            relative = Path(target).relative_to(work_dir)
            if len(relative.parts) >= 2:
                active.add(work_dir / relative.parts[0] / relative.parts[1])
    return active


def workspace_directories(work_dir: Path) -> list[Path]:
    if not work_dir.is_dir():
        return []
    return [
        workspace
        for repository_dir in work_dir.iterdir()
        if repository_dir.is_dir() and not repository_dir.is_symlink()
        for workspace in repository_dir.iterdir()
        if workspace.is_dir() and not workspace.is_symlink()
    ]


def disk_usage_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_blocks * 512
            except OSError:
                continue
    return total


def main() -> int:
    args = parse_args()
    if args.older_than_days < 0:
        raise SystemExit("--older-than-days must be non-negative")

    work_dir = args.work_dir.expanduser().resolve()
    cutoff = time.time() - args.older_than_days * 86400
    active = active_workspace_paths(work_dir)
    candidates = [
        workspace
        for workspace in workspace_directories(work_dir)
        if workspace not in active and (args.all_inactive or workspace.stat().st_mtime < cutoff)
    ]

    reclaimed = 0
    protected_during_run = 0
    for workspace in sorted(candidates):
        if workspace in active_workspace_paths(work_dir):
            protected_during_run += 1
            print(f"SKIP-ACTIVE\t0\t{workspace}")
            continue
        size = disk_usage_bytes(workspace)
        reclaimed += size
        action = "DELETE" if args.apply else "DRY-RUN"
        print(f"{action}\t{size}\t{workspace}")
        if args.apply:
            shutil.rmtree(workspace)

    print(
        f"summary\tcandidates={len(candidates)}\tprotected_active={len(active) + protected_during_run}"
        f"\tbytes={'reclaimed' if args.apply else 'would_reclaim'}={reclaimed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
