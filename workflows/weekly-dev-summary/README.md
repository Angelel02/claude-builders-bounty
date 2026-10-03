# Automated Weekly Dev Summary — n8n + Claude Code

A production-ready n8n workflow that, every Monday, collects the week's git activity
across your repositories, asks Claude Code to write a structured engineering summary,
and publishes it to Slack (with an optional email fallback).

## What it does

1. **Schedule Trigger** — fires every Monday at 09:00 (cron `0 9 * * 1`).
2. **Compute window** — computes the ISO week boundaries (`since` / `until`) for the
   reporting period so the summary is deterministic and re-runnable.
3. **Collect commits** — for each configured repo, runs `git log --since --until
   --pretty=format:...` and aggregates commits, authors, and changed files.
4. **Collect merged PRs** — calls the GitHub REST API for PRs merged in the window.
5. **Build prompt** — assembles a compact, token-bounded context document.
6. **Claude Code** — invokes the `claude` CLI in headless mode (`claude -p`) with a
   strict output contract (Markdown sections: Highlights, Shipped, In Progress,
   Risks, Metrics).
7. **Publish** — posts the summary to a Slack channel via incoming webhook; on
   failure, falls back to email via SMTP.
8. **Archive** — appends the summary to `summaries/YYYY-WW.md` in the repo for a
   permanent, greppable history.

## Files

- `weekly-dev-summary.json` — the importable n8n workflow (paste into *Workflows → Import from File*).
- `.env.example` — the credentials/configuration the workflow reads.
- `README.md` — this file.

## Setup

```bash
cp .env.example .env
# fill in GITHUB_TOKEN, SLACK_WEBHOOK_URL, REPOS, and (optionally) SMTP_*
```

Import the workflow:

```bash
# n8n CLI
n8n import:workflow --input=weekly-dev-summary.json
n8n update:workflow --id=weekly-dev-summary --active=true
```

## Configuration reference

| Variable | Purpose | Example |
|---|---|---|
| `REPOS` | Comma-separated `owner/name` list to summarise | `acme/api,acme/web` |
| `GITHUB_TOKEN` | Read-only PAT for the GitHub REST API | `ghp_...` |
| `SLACK_WEBHOOK_URL` | Incoming webhook for the summary post | `https://hooks.slack.com/...` |
| `CLAUDE_BIN` | Path to the Claude Code CLI | `claude` |
| `CLAUDE_MODEL` | Model alias passed to `claude -p` | `sonnet` |
| `SUMMARY_MAX_TOKENS` | Upper bound on the prompt context | `12000` |
| `SMTP_*` | Optional email fallback transport | — |

## Claude Code invocation contract

The workflow shells out to the Claude Code CLI in **headless / print mode**, which is
the supported way to call it non-interactively:

```bash
claude -p "$(cat prompt.md)" \
  --model "$CLAUDE_MODEL" \
  --output-format text
```

The prompt enforces a fixed Markdown skeleton so downstream consumers (Slack, the
archive file, and any dashboards) can parse sections reliably. If Claude returns a
non-zero exit code or empty output, the workflow retries **once** with a reduced
context (last 7 days only, top-20 commits) before falling back to a deterministic
template summary so the weekly report never silently disappears.

## Design notes

- **Idempotent** — the window is derived from the trigger timestamp, so re-running a
  week reproduces the same report rather than double-counting.
- **Token-bounded** — commits are truncated to `SUMMARY_MAX_TOKENS` with the most
  recent activity kept preferentially, so large monorepos don't blow the context.
- **Fail-visible** — every failure path (git, GitHub API, Claude, Slack) posts a
  structured error to the n8n error workflow; nothing fails silently.
- **No secrets in the workflow JSON** — all credentials come from the environment /
  n8n credential store, never hard-coded.

## Testing

```bash
# dry run for a fixed week without publishing
DRY_RUN=1 SINCE=2026-09-21 UNTIL=2026-09-28 n8n execute --id=weekly-dev-summary
```

The dry run prints the assembled prompt and the Claude output to stdout and skips the
Slack/email/archive steps.
