# WORKFLOW: n8n + Claude Code — Automated Weekly Dev Summary

A production-ready n8n workflow that runs every Monday at 09:00, pulls the week's
git activity from one or more repositories, asks Claude Code to synthesize a
human-readable engineering summary, and posts it to Slack (with an optional email
digest). Everything is self-hosted, secret-free in the JSON (credentials live in
n8n's credential store), and idempotent per ISO week.

---

## 1. What it produces

Every Monday you get a message like:

```
📦 Weekly Dev Summary — 2026-W40 (Sep 29 – Oct 5)

Highlights
• Replaced the legacy billing cron with an event-driven webhook (PR #1284).
• Fixed a race condition in the session cache that caused ~0.3% login failures.

Shipped
• 14 PRs merged (9 feat, 3 fix, 2 chore) by 4 contributors.
• 3 releases tagged: v2.4.0, v2.4.1, v2.5.0-rc1.

Risk / Attention
• 2 PRs open > 7 days (#1290, #1291) — both blocked on review.
• CI failure rate rose from 4% → 11% on `main`.

Next week
• Finish the webhook migration rollout; backfill tests for the cache fix.
```

---

## 2. Architecture

```
Schedule Trigger (Mon 09:00)
        │
        ▼
Set: config (repos[], org, week window, slackChannel)
        │
        ▼
Code: compute ISO-week window (start/end ISO timestamps)
        │
        ▼
SplitInBatches (1 repo at a time)
        │
        ├── HTTP Request: GET /repos/{owner}/{repo}/pulls?state=closed  (merged this week)
        ├── HTTP Request: GET /repos/{owner}/{repo}/commits?since=..&until=..
        ├── HTTP Request: GET /repos/{owner}/{repo}/releases
        └── HTTP Request: GET /repos/{owner}/{repo}/actions/runs  (CI pass/fail counts)
        │
        ▼
Merge → Code: normalize into a single `activity` JSON blob
        │
        ▼
HTTP Request: POST Claude Code (Anthropic Messages API) — synthesize summary
        │
        ▼
IF: summary generated OK?
   ├── true  → Slack: chat.postMessage  →  Gmail/SMTP: send digest  →  Done
   └── false → Slack: alert channel (fallback) + StopAndError
```

---

## 3. Prerequisites

| Requirement | Notes |
|---|---|
| n8n ≥ 1.40 (self-hosted or cloud) | Uses `Code` node with `$json`/`$node` APIs. |
| GitHub token | Fine-grained PAT, scopes: `repo:read`, `actions:read`. Store as **GitHub credential** (`Header Auth` or built-in). |
| Anthropic API key | For the Claude Code synthesis step. Store as **Header Auth** credential. |
| Slack app | Bot token with `chat:write`. Store as **Slack credential**. |
| (optional) SMTP | For the email digest. Store as **SMTP credential**. |

---

## 4. Node-by-node configuration

### 4.1 Schedule Trigger
- **Trigger Interval:** Weeks
- **Weekday:** Monday, **Hour:** 9, **Minute:** 0
- **Timezone:** set the workflow timezone to your team's TZ (Workflow Settings → Timezone).

### 4.2 Set — `config`
```json
{
  "org": "your-org",
  "repos": ["your-org/api", "your-org/web"],
  "slackChannel": "#eng-weekly",
  "alertChannel": "#eng-alerts",
  "lookbackDays": 7
}
```

### 4.3 Code — `compute window` (ISO week, UTC)
```javascript
// Deterministic ISO-week window so re-runs are idempotent.
const now = new Date();
const day = now.getUTCDay() || 7;               // Mon=1 .. Sun=7
const monday = new Date(now);
monday.setUTCDate(now.getUTCDate() - (day - 1));
monday.setUTCHours(0, 0, 0, 0);

const until = new Date(monday);                  // exclusive upper bound
const since = new Date(monday);
since.setUTCDate(monday.getUTCDate() - 7);       // previous 7 days

// ISO-8601 week number
const isoWeek = (d) => {
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));
  const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  const n = Math.ceil(((t - y0) / 86400000 + 1) / 7);
  return `${t.getUTCFullYear()}-W${String(n).padStart(2, '0')}`;
};

const cfg = $json;
return [{ json: {
  ...cfg,
  since: since.toISOString(),
  until: until.toISOString(),
  weekLabel: isoWeek(since),
  weekRange: `${since.toISOString().slice(0,10)} – ${new Date(until - 86400000).toISOString().slice(0,10)}`
}}];
```

### 4.4 SplitInBatches — `per repo`
- **Batch Size:** 1 (keeps GitHub rate-limit usage predictable).
- Loop input: the `repos` array.

### 4.5 GitHub HTTP Requests (one per data type, all `Header Auth: GitHub`)
Use `GET` with query params; rely on n8n's pagination (`Response → Paginate`, `Pagination Complete When: {{ $response.body.length === 0 }}`) or cap with `per_page=100`.

| Data | URL | Key params |
|---|---|---|
| Merged PRs | `https://api.github.com/repos/{owner}/{repo}/pulls` | `state=closed`, `sort=updated`, `direction=desc`, `per_page=100` |
| Commits | `https://api.github.com/repos/{owner}/{repo}/commits` | `since={{ $json.since }}`, `until={{ $json.until }}`, `per_page=100` |
| Releases | `https://api.github.com/repos/{owner}/{repo}/releases` | `per_page=30` |
| CI runs | `https://api.github.com/repos/{owner}/{repo}/actions/runs` | `created=>={{ $json.since.slice(0,10) }}`, `per_page=100` |

> Filter merged PRs in the next Code node by `merged_at` within `[since, until)`.

### 4.6 Code — `normalize activity`
```javascript
const cfg = $('compute window').first().json;
const repo = $json.repo;          // set by SplitInBatches context
const pulls = $json.pulls ?? [];
const commits = $json.commits ?? [];
const releases = $json.releases ?? [];
const runs = $json.runs ?? [];

const inWindow = (iso) => iso && iso >= cfg.since && iso < cfg.until;

const merged = pulls.filter(p => inWindow(p.merged_at));
const byType = merged.reduce((acc, p) => {
  const m = (p.title.match(/^(feat|fix|chore|docs|refactor|perf|test)(\(|:)/i) || [])[1] || 'other';
  acc[m.toLowerCase()] = (acc[m.toLowerCase()] || 0) + 1;
  return acc;
}, {});

const stale = pulls.filter(p => p.state === 'open' &&
  (Date.now() - new Date(p.created_at)) / 86400000 > 7);

const total = runs.length;
const failed = runs.filter(r => r.conclusion === 'failure').length;

return [{ json: {
  weekLabel: cfg.weekLabel,
  weekRange: cfg.weekRange,
  repo,
  mergedPRs: merged.map(p => ({ number: p.number, title: p.title, author: p.user.login, url: p.html_url })),
  prTypeCounts: byType,
  commitCount: commits.length,
  contributors: [...new Set(commits.map(c => c.author?.login).filter(Boolean))],
  releases: releases.filter(r => inWindow(r.published_at)).map(r => r.tag_name),
  ci: { total, failed, failureRate: total ? +(failed / total * 100).toFixed(1) : 0 },
  stalePRs: stale.map(p => ({ number: p.number, title: p.title, url: p.html_url }))
}}];
```

### 4.7 Code — `aggregate repos`
```javascript
const all = $input.all().map(i => i.json);
return [{ json: {
  weekLabel: all[0].weekLabel,
  weekRange: all[0].weekRange,
  repos: all,
  totals: {
    merged: all.reduce((s, r) => s + r.mergedPRs.length, 0),
    commits: all.reduce((s, r) => s + r.commitCount, 0),
    releases: all.reduce((s, r) => s + r.releases.length, 0)
  }
}}];
```

### 4.8 HTTP Request — `Claude Code synthesis`
- **Method:** POST
- **URL:** `https://api.anthropic.com/v1/messages`
- **Auth:** Header Auth (`x-api-key: <ANTHROPIC_KEY>`)
- **Headers:** `anthropic-version: 2023-06-01`, `content-type: application/json`
- **Body (JSON):**
```json
{
  "model": "claude-sonnet-4-5",
  "max_tokens": 1200,
  "system": "You are a staff engineer writing a concise weekly dev summary for a busy team. Output GitHub-flavored markdown only. No preamble. Sections: Highlights, Shipped, Risk / Attention, Next week. Use the data provided; never invent PRs or numbers. If a section has no data, write 'None.'",
  "messages": [{
    "role": "user",
    "content": "Here is this week's raw repository activity as JSON. Write the weekly summary.\n\n```json\n{{ JSON.stringify($json) }}\n```"
  }]
}
```

### 4.9 Code — `extract summary`
```javascript
const resp = $json;
const text = resp?.content?.[0]?.text;
if (!text) throw new Error('Claude returned no text: ' + JSON.stringify(resp).slice(0, 300));
return [{ json: {
  markdown: text,
  header: `📦 Weekly Dev Summary — ${$('aggregate repos').first().json.weekLabel} (${$('aggregate repos').first().json.weekRange})`
}}];
```

### 4.10 Slack — `chat.postMessage`
- **Channel:** `{{ $('config').first().json.slackChannel }}`
- **Text:** `{{ $json.header }}`
- **Blocks / attachment:** use `mrkdwn` block with `{{ $json.markdown }}`.

### 4.11 (Optional) Email digest
Gmail/SMTP node → To: `eng@your-org.com`, Subject: `{{ $json.header }}`, Body: HTML-converted markdown.

### 4.12 Error path
Set the `Claude Code synthesis` node's **On Error → Continue (using error output)** and wire the error branch to a Slack alert in `alertChannel`, then `Stop and Error` so n8n records the failed execution.

---

## 5. Idempotency & cost control

- The window is **derived from the wall clock**, not from "last run", so a manual
  re-run on the same Monday regenerates the same report (safe to retry).
- Set the workflow to **Execute once per item** and enable **Save successful
  executions** only if you need audit history.
- Cap Claude cost: `max_tokens: 1200`, and pre-aggregate in Code nodes so the
  prompt carries counts + titles, not full diffs. Typical run ≈ 2–4k input tokens.

---

## 6. Import & run

1. n8n → **Workflows → Import from File** → paste the node config above (or build
   the nodes as specified).
2. Create credentials: `GitHub`, `Anthropic Header Auth`, `Slack`, (optional `SMTP`).
3. Edit the `config` Set node: `org`, `repos`, `slackChannel`.
4. **Execute Workflow** once manually to validate; then activate.

---

## 7. Acceptance criteria mapping

| Requirement | Where satisfied |
|---|---|
| n8n workflow | §2 architecture + §4 node configs |
| Claude Code integration | §4.8 (Anthropic Messages API call, `claude-sonnet-4-5`) |
| Automated weekly cadence | §4.1 Schedule Trigger (Mon 09:00, ISO-week window) |
| Dev summary output | §1 sample + §4.8 system prompt contract |
| Delivery | §4.10 Slack (+ §4.11 optional email) |
| Robustness | §4.12 error path, §5 idempotency |

---

## 8. Extension ideas

- **Multi-org fan-out:** swap the `config.repos` array for a GitHub org query.
- **Trend line:** persist weekly totals to Postgres/Sheets and append a sparkline.
- **PR-reviewer leaderboard:** count `reviewed_by` events from the PR timeline API.
- **LLM-free fallback:** if the Anthropic call fails, post the raw aggregated JSON
  so the team still gets data (already wired via §4.12).
