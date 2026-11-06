#!/usr/bin/env python3
"""Refuse to stage event-only evaluation material.

The credit-union snapshots, manifests, harness, public eval, PDFs, and
quotation-heavy QA extracts are event-only. This script is the repository
check used by .githooks/pre-commit. It does not modify git state.

Verify without committing::

    python3 scripts/guard_staged_paths.py --self-test
    git diff --cached --name-only | python3 scripts/guard_staged_paths.py --stdin
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Path fragments and suffixes that must not enter history.
BLOCKED_PARTS = (
    "snapshots",
    "snapshots_zip",
    "eval",
    "harness",
    "participant-kit",
    "participant_kit",
    "private-review",
    "uploads",
    "results",
    ".venv",
    "venv",
    "__pycache__",
)
BLOCKED_SUFFIXES = (".zip", ".pdf", ".jsonl")
BLOCKED_NAMES = {
    "manifest.json",
    ".env",
    "public_eval.jsonl",
    "conflicts_gold.json",
    "proposed-tests.json",
    "qa-report.md",
    "temporal-evidence.txt",
    "source-key.json",
}
BLOCKED_NAME_FRAGMENTS = (
    "public_eval",
    "conflicts_gold",
    "proposed-tests",
    "qa-report",
    "temporal-evidence",
    "source-key",
    "snapshot",
)


def _reason(path: str) -> str | None:
    rel = path.replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return None
    parts = [p.lower() for p in rel.split("/") if p and p != "."]
    name = parts[-1] if parts else ""
    for part in parts:
        if part in BLOCKED_PARTS or part.startswith("snapshot"):
            return f"blocked directory '{part}'"
        if part.startswith("0") and "_20" in part and len(part) > 8:
            return "blocked snapshot folder name"
    if name in BLOCKED_NAMES:
        return f"blocked filename '{name}'"
    for frag in BLOCKED_NAME_FRAGMENTS:
        if frag in name and name not in {"guard_staged_paths.py"}:
            # Allow this script and NOTES to mention the words; block data files.
            if name.endswith((".py", ".md")) and frag not in {
                "public_eval",
                "conflicts_gold",
                "proposed-tests",
                "qa-report",
                "temporal-evidence",
                "source-key",
            }:
                continue
            if frag in name and not name.endswith(".py"):
                return f"blocked name fragment '{frag}'"
    for suf in BLOCKED_SUFFIXES:
        if name.endswith(suf):
            return f"blocked suffix '{suf}'"
    if name.startswith(".env"):
        return "blocked env file"
    return None


def staged_paths() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "git diff --cached failed\n")
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        samples = {
            "solution.py": None,
            "NOTES.md": None,
            "tests/test_synthetic.py": None,
            "snapshots/01_2026-05-12_baseline/manifest.json": "blocked",
            "eval/public_eval.jsonl": "blocked",
            "harness/scoring.py": "blocked",
            "uploads/assignment.pdf": "blocked",
            "private-review/temporal-evidence.txt": "blocked",
            "kit.zip": "blocked",
            "results/report.json": "blocked",
            ".env": "blocked",
        }
        failed = False
        for path, expect in samples.items():
            got = _reason(path)
            ok = (got is None) if expect is None else (got is not None)
            if not ok:
                sys.stderr.write(f"self-test failed for {path}: got {got!r}, expect {expect}\n")
                failed = True
        if failed:
            return 1
        print("guard self-test passed")
        return 0

    if "--stdin" in argv:
        paths = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    elif len(argv) > 1 and not argv[1].startswith("-"):
        paths = argv[1:]
    else:
        paths = staged_paths()

    bad = [(p, _reason(p)) for p in paths if _reason(p)]
    if not bad:
        return 0
    sys.stderr.write("Refusing to stage event-only or generated evaluation files:\n")
    for path, why in bad:
        sys.stderr.write(f"  {path}  ({why})\n")
    sys.stderr.write(
        "Keep snapshots, eval, harness, PDFs, and QA extracts out of git.\n"
        "See NOTES.md for how this guard is installed.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
