#!/usr/bin/env python3
"""
PreToolUse hook for Claude Code: blocks destructive bash commands.

Install (settings.json):
    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Bash",
            "hooks": [
              {"type": "command", "command": "python3 /path/to/block_destructive_bash.py"}
            ]
          }
        ]
      }
    }

Behavior:
  * Reads the tool-call JSON payload from stdin (Claude Code passes
    {"tool_name": "Bash", "tool_input": {"command": "..."}}).
  * If the command matches a destructive pattern, it exits with code 2 and
    prints a JSON decision that DENIES the call, explaining why.
  * Otherwise it exits 0 and allows the command (optionally emitting an
    "allow" decision).

Exit codes (Claude Code hook contract):
  0 = allow (stdout JSON may carry a decision)
  2 = block (stderr is fed back to the model as the reason)
"""
from __future__ import annotations

import json
import re
import sys

# --- Destructive pattern definitions ---------------------------------------
# Each entry: (compiled regex, short human-readable reason).
# Patterns are intentionally conservative to avoid false positives on
# read-only commands, but strict enough to stop catastrophic operations.
DESTRUCTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Recursive force delete of root / home / wildcards
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*[rR]"),
     "recursive force delete (rm -rf)"),
    (re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*\s+/(\s|$|\*)"),
     "force delete of filesystem root"),
    (re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\$HOME|\*)"),
     "recursive delete of root/home/wildcard"),

    # Disk / filesystem destruction
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format (mkfs)"),
    (re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|hd|vd)"), "raw disk write (dd of=/dev/...)"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd)\w"), "redirect overwrite of block device"),
    (re.compile(r"\b(shred|wipefs)\b"), "irreversible disk wipe (shred/wipefs)"),

    # Fork bombs
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:"), "fork bomb"),
    (re.compile(r"\bwhile\s+true\b.*\bdo\b.*\b(fork|&\s*done)"), "potential fork bomb"),

    # Permission / ownership catastrophe on system paths
    (re.compile(r"\bchmod\s+-R\s+0?777\s+/(\s|$)"), "chmod 777 on filesystem root"),
    (re.compile(r"\bchown\s+-R\b.*\s/(\s|$)"), "recursive chown of filesystem root"),

    # Git history destruction / remote force-push
    (re.compile(r"\bgit\s+push\b.*(--force|-f)\b.*\b(main|master|production|release)\b"),
     "force-push to protected branch"),
    (re.compile(r"\bgit\s+reset\s+--hard\b.*\bHEAD~"), "hard reset discarding commits"),
    (re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*[fF][a-zA-Z]*[dD]"), "git clean -fd (removes untracked files)"),

    # Package/registry destructive operations
    (re.compile(r"\bnpm\s+(unpublish|publish\s+--force)\b"), "npm unpublish / force publish"),
    (re.compile(r"\bpip\s+uninstall\b.*\s-y\b.*\ball\b"), "pip uninstall all"),

    # Curl|bash remote code execution from untrusted source
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(bash|sh|zsh)\b"),
     "pipe remote script directly into a shell"),

    # Overwriting critical system files
    (re.compile(r">\s*/(etc|boot|sys|proc)/"), "overwrite of critical system path"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "system shutdown/reboot"),

    # Database catastrophic drops
    (re.compile(r"\bDROP\s+(DATABASE|TABLE)\b", re.IGNORECASE), "SQL DROP DATABASE/TABLE"),
    (re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE), "SQL TRUNCATE TABLE"),
]

# Commands that are safe even if they contain a risky-looking token.
ALLOWLIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(ls|cat|grep|rg|find|echo|pwd|git\s+status|git\s+log)\b"),
]


def extract_command(payload: dict) -> str:
    """Best-effort extraction of the bash command string from the payload."""
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "script"):
            val = tool_input.get(key)
            if isinstance(val, str):
                return val
    # Some Claude Code versions pass the command directly.
    direct = payload.get("command")
    return direct if isinstance(direct, str) else ""


def is_allowlisted(command: str) -> bool:
    return any(p.match(command) for p in ALLOWLIST_PATTERNS)


def find_violation(command: str) -> str | None:
    """Return the reason string if the command is destructive, else None."""
    if not command.strip():
        return None
    if is_allowlisted(command):
        return None
    for pattern, reason in DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return reason
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Fail open on malformed input but warn on stderr.
        print("block_destructive_bash: could not parse hook payload; allowing.",
              file=sys.stderr)
        return 0

    command = extract_command(payload)
    reason = find_violation(command)

    if reason is None:
        # Explicit allow decision (harmless; Claude Code treats exit 0 as allow).
        print(json.dumps({"decision": "allow"}))
        return 0

    # Block: exit code 2 + JSON decision. stderr reason is shown to the model.
    message = (
        f"BLOCKED: destructive command detected ({reason}). "
        f"Command: {command.strip()[:200]}"
    )
    print(json.dumps({
        "decision": "block",
        "reason": message,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
    }))
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
