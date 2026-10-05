#!/usr/bin/env python3
"""Environment Studio version and build metadata.

A packaged build writes build-info.json next to the application; a source
checkout has none, and its build info comes from git.

  python tools/env_version.py        print the build info as JSON
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parents[1]
BUILD_INFO_NAME = "build-info.json"


def git_commit(root: Path = ROOT) -> str | None:
    """The checkout's commit SHA, or None without git or a repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
            # No console window pops up on Windows (the Studio runs without one).
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def build_info(root: Path = ROOT) -> dict:
    """{version, commit, build_date, platform}: the package's, or the checkout's."""
    path = root / BUILD_INFO_NAME
    if path.is_file():
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = None
        if isinstance(info, dict):
            return info
    return {"version": VERSION, "commit": git_commit(root), "build_date": None, "platform": "source"}


def describe(info: dict) -> str:
    commit = (info.get("commit") or "unknown")[:12]
    built = info.get("build_date") or "not packaged"
    return f"Environment Studio {info.get('version', VERSION)} ({info.get('platform', 'source')}, {commit}, built {built})"


if __name__ == "__main__":
    json.dump(build_info(), sys.stdout, indent=2, ensure_ascii=False)
    print()
