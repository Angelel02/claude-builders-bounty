#!/usr/bin/env python3
"""
Claude Code SKILL: generate a structured CHANGELOG from git history.

Usage:
    python3 changelog.py [--from <ref>] [--to <ref>] [--version X.Y.Z]
                         [--repo <path>] [--out CHANGELOG.md]

Groups commits between two refs by Conventional-Commit type and renders a
Keep-a-Changelog formatted section. Falls back to a heuristic classifier for
non-conventional commit messages so it works on any repo.

Example:
    python3 changelog.py --from v1.2.0 --to HEAD --version 1.3.0
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from collections import defaultdict

# Conventional-commit type -> CHANGELOG section heading (Keep a Changelog order).
TYPE_TO_SECTION = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Performance",
    "refactor": "Changed",
    "docs": "Documentation",
    "style": "Changed",
    "test": "Tests",
    "build": "Build",
    "ci": "CI",
    "chore": "Chores",
    "revert": "Reverted",
}
SECTION_ORDER = [
    "Added", "Changed", "Fixed", "Performance", "Reverted",
    "Documentation", "Tests", "Build", "CI", "Chores", "Other",
]

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s*(?P<desc>.+)$"
)
# Heuristic fallback keywords when the message is not conventional.
HEURISTIC = [
    (re.compile(r"\b(add|adds|added|introduce|implement|support)\b", re.I), "feat"),
    (re.compile(r"\b(fix|fixes|fixed|bug|patch|resolve|resolves)\b", re.I), "fix"),
    (re.compile(r"\b(refactor|rename|move|reorganize)\b", re.I), "refactor"),
    (re.compile(r"\b(doc|docs|readme|comment)\b", re.I), "docs"),
    (re.compile(r"\b(test|tests|spec)\b", re.I), "test"),
    (re.compile(r"\b(perf|performance|optimi[sz]e|speed)\b", re.I), "perf"),
    (re.compile(r"\b(ci|pipeline|workflow|build)\b", re.I), "ci"),
]


def run_git(args: list[str], repo: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def collect_commits(repo: str, from_ref: str | None, to_ref: str) -> list[tuple[str, str]]:
    """Return list of (short_sha, subject) between refs."""
    # Unit-separator delimiter to split sha and subject safely.
    fmt = "%h\x1f%s"
    rng = f"{from_ref}..{to_ref}" if from_ref else to_ref
    out = run_git(["log", rng, f"--pretty=format:{fmt}"], repo)
    commits: list[tuple[str, str]] = []
    for line in out.splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            commits.append((sha.strip(), subject.strip()))
    return commits


def classify(subject: str) -> tuple[str, str, bool]:
    """Return (section, cleaned_description, is_breaking)."""
    m = CONVENTIONAL_RE.match(subject)
    if m:
        ctype = m.group("type").lower()
        desc = m.group("desc").strip()
        breaking = bool(m.group("breaking"))
        section = TYPE_TO_SECTION.get(ctype, "Other")
        return section, desc, breaking
    for pattern, ctype in HEURISTIC:
        if pattern.search(subject):
            return TYPE_TO_SECTION.get(ctype, "Other"), subject, False
    return "Other", subject, False


def render(version: str | None, date: str, commits: list[tuple[str, str]]) -> str:
    buckets: dict[str, list[str]] = defaultdict(list)
    breaking_notes: list[str] = []

    for sha, subject in commits:
        section, desc, breaking = classify(subject)
        entry = f"- {desc} ({sha})"
        buckets[section].append(entry)
        if breaking:
            breaking_notes.append(f"- {desc} ({sha})")

    header = f"## [{version or 'Unreleased'}] - {date}"
    lines = [header, ""]

    for section in SECTION_ORDER:
        if buckets.get(section):
            lines.append(f"### {section}")
            lines.extend(buckets[section])
            lines.append("")

    if breaking_notes:
        lines.append("### ⚠ BREAKING CHANGES")
        lines.extend(breaking_notes)
        lines.append("")

    if not any(buckets.values()):
        lines.append("_No notable changes._")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a structured CHANGELOG from git history.")
    ap.add_argument("--repo", default=".", help="Path to the git repository.")
    ap.add_argument("--from", dest="from_ref", default=None,
                    help="Start ref (exclusive). Omit to include full history.")
    ap.add_argument("--to", dest="to_ref", default="HEAD", help="End ref (inclusive).")
    ap.add_argument("--version", default=None, help="Version label, e.g. 1.3.0.")
    ap.add_argument("--date", default=_dt.date.today().isoformat(), help="Release date.")
    ap.add_argument("--out", default=None, help="Write to file instead of stdout.")
    args = ap.parse_args(argv)

    try:
        commits = collect_commits(args.repo, args.from_ref, args.to_ref)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not commits:
        print("warning: no commits found in range.", file=sys.stderr)

    changelog = render(args.version, args.date, commits)

    if args.out:
        existing = ""
        try:
            with open(args.out, "r", encoding="utf-8") as fh:
                existing = fh.read()
        except FileNotFoundError:
            existing = "# Changelog\n\nAll notable changes to this project.\n\n"
        # Insert the new section after the top-level preamble.
        if existing.startswith("# Changelog"):
            preamble, _, rest = existing.partition("\n\n")
            merged = f"{preamble}\n\n{changelog}\n{rest.lstrip()}"
        else:
            merged = changelog + "\n" + existing
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(merged)
        print(f"CHANGELOG written to {args.out}")
    else:
        sys.stdout.write(changelog)
    return 0


if __name__ == "__main__":
    sys.exit(main())
