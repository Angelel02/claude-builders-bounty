#!/usr/bin/env python3
"""
Pre-tool-use hook for Claude Code that blocks destructive bash commands.

Wired into Claude Code via .claude/settings.json:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              { "type": "command",
                "command": "python3 .claude/hooks/block-destructive-bash.py" }
            ]
          }
        ]
      }
    }

Contract (Claude Code hook protocol):
  * The hook receives the pending tool call as JSON on stdin:
        { "tool_name": "Bash", "tool_input": { "command": "rm -rf /" } }
  * Exit code 0  -> allow the command to run.
  * Exit code 2  -> BLOCK the command; stderr is surfaced back to the model
                   so it can choose a safer alternative.

Design goals:
  1. Fail CLOSED on anything we cannot confidently classify as safe.
  2. No shell=True, no eval, no subprocess execution of the inspected string.
  3. Deterministic, dependency-free (stdlib only) so it runs anywhere.
  4. Explain *why* a command was blocked, not just that it was.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Iterable


# --------------------------------------------------------------------------- #
# Rule model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Rule:
    """A single destructive-command heuristic."""

    name: str
    pattern: re.Pattern[str]
    reason: str


def _rx(expr: str) -> re.Pattern[str]:
    # Case-insensitive, dot-matches-newline, verbose for readability.
    return re.compile(expr, re.IGNORECASE | re.DOTALL | re.VERBOSE)


RULES: tuple[Rule, ...] = (
    # -- Filesystem destruction -------------------------------------------- #
    Rule(
        "rm-rf-root",
        _rx(r"""
            \brm\b
            (?=[^\n|;&]*(?:\s-[a-z]*r[a-z]*|\s--recursive))
            (?=[^\n|;&]*(?:\s-[a-z]*f[a-z]*|\s--force))
            [^\n|;&]*
            (?:\s|^)(?:/|/\*|~|~/\*|\$HOME|\$\{HOME\})(?:\s|$|\*)
        """),
        "Recursive force-delete targeting the filesystem root or $HOME.",
    ),
    Rule(
        "rm-rf-broad",
        _rx(r"""
            \brm\b
            (?=[^\n|;&]*(?:\s-[a-z]*r[a-z]*|\s--recursive))
            (?=[^\n|;&]*(?:\s-[a-z]*f[a-z]*|\s--force))
            [^\n|;&]*
            (?:\s|^)(?:\*|\.|\./|\*/\*)(?:\s|$)
        """),
        "Recursive force-delete of the current directory or a bare glob.",
    ),
    Rule(
        "mkfs",
        _rx(r"\bmkfs(?:\.\w+)?\b"),
        "Formats a filesystem (mkfs), destroying all data on the device.",
    ),
    Rule(
        "dd-to-device",
        _rx(r"\bdd\b[^\n|;&]*\bof=/dev/(?:sd|nvme|hd|vd|disk|mmcblk)"),
        "Raw block write (dd) directly to a disk device.",
    ),
    Rule(
        "redirect-to-device",
        _rx(r">\s*/dev/(?:sd|nvme|hd|vd|disk|mmcblk)\w*"),
        "Shell redirection overwriting a raw disk device.",
    ),
    Rule(
        "wipefs",
        _rx(r"\bwipefs\b"),
        "Erases filesystem signatures from a device (wipefs).",
    ),
    Rule(
        "shred",
        _rx(r"\bshred\b"),
        "Irreversibly overwrites files (shred).",
    ),

    # -- Permission / ownership footguns ----------------------------------- #
    Rule(
        "chmod-777-root",
        _rx(r"\bchmod\b[^\n|;&]*\b777\b[^\n|;&]*\s/(?:\s|$)"),
        "chmod 777 applied to the filesystem root.",
    ),
    Rule(
        "chmod-recursive-root",
        _rx(r"\bchmod\b[^\n|;&]*(?:-R|--recursive)[^\n|;&]*\s/(?:\s|$)"),
        "Recursive permission change across the filesystem root.",
    ),
    Rule(
        "chown-recursive-root",
        _rx(r"\bchown\b[^\n|;&]*(?:-R|--recursive)[^\n|;&]*\s/(?:\s|$)"),
        "Recursive ownership change across the filesystem root.",
    ),

    # -- Privilege escalation ---------------------------------------------- #
    Rule(
        "chmod-setuid",
        _rx(r"\bchmod\b[^\n|;&]*(?:[ug]\+s|4[0-7]{3}\b)"),
        "Sets the SUID/SGID bit, enabling privilege escalation.",
    ),

    # -- Remote code execution / exfiltration ------------------------------ #
    Rule(
        "curl-pipe-shell",
        _rx(r"""
            \b(?:curl|wget)\b[^\n|;&]*
            \|\s*
            (?:sudo\s+)?
            (?:ba|z|k|da)?sh\b
        """),
        "Pipes a remote download straight into a shell (curl|sh).",
    ),
    Rule(
        "eval-remote",
        _rx(r"\beval\b[^\n|;&]*\$\(\s*(?:curl|wget)\b"),
        "eval() of remotely fetched content.",
    ),
    Rule(
        "base64-pipe-shell",
        _rx(r"\bbase64\b[^\n|;&]*-d[^\n|;&]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b"),
        "Decodes base64 and pipes it into a shell (obfuscated payload).",
    ),

    # -- Fork bombs / resource exhaustion ---------------------------------- #
    Rule(
        "fork-bomb",
        _rx(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        "Classic fork bomb.",
    ),
    Rule(
        "fork-bomb-variant",
        _rx(r"\b(?:perl|python3?|ruby)\b[^\n|;&]*fork[^\n|;&]*while\s*\(?\s*(?:true|1)\b"),
        "Fork loop that exhausts process table / memory.",
    ),

    # -- History / git destruction ----------------------------------------- #
    Rule(
        "git-push-force-protected",
        _rx(r"\bgit\s+push\b[^\n|;&]*(?:--force|-f)\b[^\n|;&]*\b(?:main|master|release|production|prod)\b"),
        "Force-push to a protected branch, rewriting shared history.",
    ),
    Rule(
        "git-reset-hard-remote",
        _rx(r"\bgit\s+reset\s+--hard\s+origin/"),
        "Hard reset to a remote ref, discarding local commits.",
    ),
    Rule(
        "git-clean-force",
        _rx(r"\bgit\s+clean\b[^\n|;&]*(?:-[a-z]*f[a-z]*d|-[a-z]*d[a-z]*f)"),
        "Force-cleans untracked files and directories (git clean -fd).",
    ),

    # -- Database / infra destruction -------------------------------------- #
    Rule(
        "drop-database",
        _rx(r"\bDROP\s+(?:DATABASE|SCHEMA|TABLE)\b"),
        "SQL DROP statement destroying schema or data.",
    ),
    Rule(
        "truncate-table",
        _rx(r"\bTRUNCATE\s+TABLE\b"),
        "SQL TRUNCATE removing all rows from a table.",
    ),
    Rule(
        "kubectl-delete-namespace",
        _rx(r"\bkubectl\s+delete\s+(?:ns|namespace|all)\b"),
        "Deletes a Kubernetes namespace or all resources.",
    ),
    Rule(
        "docker-prune-all",
        _rx(r"\bdocker\s+(?:system|volume|image)\s+prune\b[^\n|;&]*(?:-a|--all)"),
        "Prunes all Docker images/volumes (docker prune -a).",
    ),

    # -- Cloud / infra mass-delete ----------------------------------------- #
    Rule(
        "aws-s3-rb-force",
        _rx(r"\baws\s+s3\s+rb\b[^\n|;&]*--force"),
        "Force-deletes an S3 bucket and all of its objects.",
    ),
    Rule(
        "terraform-destroy-auto",
        _rx(r"\bterraform\s+destroy\b[^\n|;&]*(?:-auto-approve|--auto-approve)"),
        "Auto-approved Terraform destroy of managed infrastructure.",
    ),
)

# Commands that are inherently safe even if they look risky to a regex, used
# to reduce false positives (e.g. `rm -rf ./build` inside a project dir).
SAFE_PREFIXES: tuple[re.Pattern[str], ...] = (
    _rx(r"^\s*rm\s+-rf\s+\./(?:build|dist|out|tmp|node_modules|target|\.cache)\s*$"),
)


def _strip_quoted_regions(cmd: str) -> str:
    """
    Remove single/double-quoted regions so heuristics do not fire on strings
    that merely *mention* a dangerous pattern (e.g. echo "rm -rf /").
    """
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in cmd:
        if escaped:
            escaped = False
            if quote is None:
                out.append(ch)
            continue
        if ch == "\\":
            escaped = True
            if quote is None:
                out.append(ch)
            continue
        if quote is None and ch in ("'", '"'):
            quote = ch
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        out.append(ch)
    return "".join(out)


def find_violations(command: str) -> list[Rule]:
    """Return every rule that the given command violates."""
    if not command or not command.strip():
        return []

    inspected = _strip_quoted_regions(command)

    for safe in SAFE_PREFIXES:
        if safe.match(command.strip()):
            return []

    return [rule for rule in RULES if rule.pattern.search(inspected)]


def evaluate(payload: dict) -> tuple[int, str]:
    """
    Evaluate a hook payload.

    Returns (exit_code, message). exit_code == 2 blocks the command.
    """
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}

    if tool_name != "Bash":
        return 0, ""

    command = str(tool_input.get("command", ""))

    # Fail closed: a Bash call with no inspectable command is suspicious.
    if not command.strip():
        return 2, "Blocked: empty Bash command could not be verified as safe."

    violations = find_violations(command)
    if not violations:
        return 0, ""

    lines = [
        "BLOCKED by block-destructive-bash hook: this command matches "
        f"{len(violations)} destructive pattern(s).",
        "",
    ]
    for rule in violations:
        lines.append(f"  - [{rule.name}] {rule.reason}")
    lines += [
        "",
        "The command was not executed. Choose a safer alternative:",
        "  * scope deletions to an explicit, non-root path",
        "  * use --dry-run / --interactive before any destructive flag",
        "  * back up data before overwriting or dropping it",
        "  * download, inspect, then execute scripts instead of piping to a shell",
    ]
    return 2, "\n".join(lines)


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception as exc:  # pragma: no cover - stdin failure is fatal
        print(f"block-destructive-bash: could not read stdin: {exc}", file=sys.stderr)
        return 2

    if not raw.strip():
        # No payload: nothing to inspect -> allow (non-Bash invocations).
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Malformed input: fail closed rather than silently allowing.
        print(f"block-destructive-bash: invalid hook JSON: {exc}", file=sys.stderr)
        return 2

    code, message = evaluate(payload)
    if code != 0 and message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
