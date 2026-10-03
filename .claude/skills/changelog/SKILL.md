# SKILL: Generate a structured CHANGELOG from git history

## Purpose

Turn raw `git log` output into a clean, human-readable `CHANGELOG.md` that
follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and
[Semantic Versioning](https://semver.org/). The skill groups commits by type,
attributes them to releases, and links each entry back to its commit.

## When to use

Use this skill whenever the user asks to:

- "generate a changelog", "update CHANGELOG.md", "what changed since vX?"
- prepare release notes for a tag or an upcoming version
- summarize a commit range (`v1.2.0..HEAD`) for a PR description

## Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `range` | no | last tag `..HEAD` | Git revision range, e.g. `v1.1.0..v1.2.0` |
| `version` | no | `Unreleased` | Version heading to write |
| `output` | no | `CHANGELOG.md` | Output file path |
| `repo` | no | cwd | Path to the git repository |

## Procedure

1. **Locate the range.** If no range is given, find the most recent tag:
   ```bash
   git describe --tags --abbrev=0 2>/dev/null || echo ""
   ```
   Use `<last-tag>..HEAD`; if there are no tags, use the full history.

2. **Extract commits** with a machine-readable delimiter so messages that span
   multiple lines survive intact:
   ```bash
   git log <range> \
     --no-merges \
     --pretty=format:'%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%b%x1e'
   ```
   `%x1f` (unit separator) separates fields, `%x1e` (record separator)
   separates commits.

3. **Classify each commit** by its Conventional Commits prefix:

   | Prefix | CHANGELOG section |
   |--------|-------------------|
   | `feat` | Added |
   | `fix` | Fixed |
   | `perf` | Changed |
   | `refactor`, `style` | Changed |
   | `docs` | Documentation |
   | `build`, `ci`, `chore` | Maintenance |
   | `test` | Testing |
   | `revert` | Removed |
   | *(none)* | Other |

   A `!` after the type (e.g. `feat!:`) or a `BREAKING CHANGE:` footer marks
   the entry as **breaking** and it is hoisted to the top of the release.

4. **Detect scope.** A leading `type(scope):` yields the scope, rendered as
   `**scope:** description`.

5. **De-duplicate and order.** Sort sections in the canonical order
   (Added, Changed, Fixed, Removed, Documentation, Maintenance, Testing,
   Other). Within a section, order by commit date ascending.

6. **Render** the release block and prepend it to the existing changelog,
   preserving prior releases verbatim.

7. **Verify** the file parses as Markdown and that every commit in the range
   appears exactly once.

## Output format

```markdown
## [1.2.0] - 2026-10-03

### ⚠ BREAKING CHANGES
- **api:** remove deprecated `/v1/tokens` endpoint ([a1b2c3d])

### Added
- **cli:** `--dry-run` flag for the deploy command ([d4e5f6a])
- support for `.env.local` overrides ([b7c8d9e])

### Fixed
- **parser:** handle CRLF line endings ([f0a1b2c])

### Changed
- **deps:** bump `httpx` to 0.28.0 ([3c4d5e6])

[1.2.0]: https://github.com/OWNER/REPO/compare/v1.1.0...v1.2.0
```

## Reference implementation

```python
#!/usr/bin/env python3
"""Generate a Keep-a-Changelog CHANGELOG.md from git history."""
from __future__ import annotations

import argparse
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

SECTION_FOR_TYPE = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Changed",
    "refactor": "Changed",
    "style": "Changed",
    "docs": "Documentation",
    "build": "Maintenance",
    "ci": "Maintenance",
    "chore": "Maintenance",
    "test": "Testing",
    "revert": "Removed",
}
SECTION_ORDER = [
    "Added", "Changed", "Fixed", "Removed",
    "Documentation", "Maintenance", "Testing", "Other",
]


@dataclass
class Commit:
    sha: str
    short: str
    author: str
    date: str
    subject: str
    body: str

    @property
    def breaking(self) -> bool:
        return "BREAKING CHANGE:" in self.body or self.subject.startswith(
            tuple(f"{t}!" for t in SECTION_FOR_TYPE)
        )

    @property
    def ctype(self) -> str:
        head = self.subject.split(":", 1)[0].split("(", 1)[0].rstrip("!")
        return head if head in SECTION_FOR_TYPE else ""

    @property
    def scope(self) -> str:
        if "(" in self.subject and ")" in self.subject:
            return self.subject.split("(", 1)[1].split(")", 1)[0]
        return ""

    @property
    def description(self) -> str:
        return self.subject.split(":", 1)[1].strip() if ":" in self.subject else self.subject


def git(*args: str, repo: str | None = None) -> str:
    cmd = ["git"]
    if repo:
        cmd += ["-C", repo]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def last_tag(repo: str | None) -> str:
    try:
        return git("describe", "--tags", "--abbrev=0", repo=repo).strip()
    except subprocess.CalledProcessError:
        return ""


def collect(range_: str, repo: str | None) -> list[Commit]:
    raw = git(
        "log", range_, "--no-merges",
        "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%b%x1e",
        repo=repo,
    )
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        sha, short, author, dt, subject = parts[:5]
        body = parts[5] if len(parts) > 5 else ""
        commits.append(Commit(sha, short, author, dt, subject, body))
    return commits


def render(commits: list[Commit], version: str, repo_url: str = "") -> str:
    sections: dict[str, list[str]] = defaultdict(list)
    breaking: list[str] = []

    for c in commits:
        scope = f"**{c.scope}:** " if c.scope else ""
        entry = f"- {scope}{c.description} ([{c.short}])"
        if c.breaking:
            breaking.append(entry)
        sections[SECTION_FOR_TYPE.get(c.ctype, "Other")].append(entry)

    lines = [f"## [{version}] - {date.today().isoformat()}", ""]
    if breaking:
        lines += ["### \u26a0 BREAKING CHANGES", *breaking, ""]
    for name in SECTION_ORDER:
        if sections.get(name):
            lines += [f"### {name}", *sections[name], ""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", dest="range_", default="")
    ap.add_argument("--version", default="Unreleased")
    ap.add_argument("--output", default="CHANGELOG.md")
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()

    rng = args.range_
    if not rng:
        tag = last_tag(args.repo)
        rng = f"{tag}..HEAD" if tag else "HEAD"

    commits = collect(rng, args.repo)
    block = render(commits, args.version)

    try:
        with open(args.output, encoding="utf-8") as fh:
            existing = fh.read()
    except FileNotFoundError:
        existing = "# Changelog\n\nAll notable changes to this project.\n\n"

    header, _, rest = existing.partition("\n\n")
    new = f"{header}\n\n{block}\n{rest.lstrip()}"
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"Wrote {len(commits)} commits to {args.output}")


if __name__ == "__main__":
    main()
```

## Pitfalls

- **Merge commits** are skipped (`--no-merges`); they duplicate their children.
- **Multi-line bodies** must be captured with a record separator, not `%n`.
- **Reverts** reference the original SHA in the body — link both.
- **No tags yet**: fall back to full history and label it `Unreleased`.
- Never rewrite existing release sections; only prepend.

## Verification checklist

- [ ] Every commit in the range appears exactly once.
- [ ] Breaking changes are hoisted above all sections.
- [ ] Section order matches `SECTION_ORDER`.
- [ ] `CHANGELOG.md` is valid Markdown and prior releases are untouched.
