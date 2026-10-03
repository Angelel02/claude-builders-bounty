# CLAUDE.md — Next.js + SQLite SaaS

Guidance for Claude Code when working in this repository.

## Project overview

A multi-tenant SaaS starter built on **Next.js (App Router)** with **SQLite**
as the primary datastore. Chosen for zero-ops local development and
single-file deploys; production runs the same schema on a persistent volume.

- **Framework:** Next.js 15 (App Router, React Server Components)
- **Language:** TypeScript (strict)
- **Database:** SQLite via `better-sqlite3` + **Drizzle ORM**
- **Auth:** session cookies (httpOnly, SameSite=Lax), Argon2id password hashing
- **Styling:** Tailwind CSS + shadcn/ui
- **Testing:** Vitest (unit) + Playwright (e2e)
- **Package manager:** pnpm

## Repository layout

```
app/                  # routes (App Router)
  (marketing)/        # public pages
  (app)/              # authenticated dashboard
  api/                # route handlers
db/
  schema.ts           # Drizzle table definitions
  migrations/         # generated SQL migrations
  client.ts           # singleton connection
lib/
  auth/               # session, password, guards
  billing/            # Stripe integration
  db/                 # queries & transactions
components/           # shared UI
scripts/              # seed, migrate, backup
```

## Commands

```bash
pnpm dev              # start dev server (http://localhost:3000)
pnpm build            # production build
pnpm lint             # eslint
pnpm typecheck        # tsc --noEmit
pnpm test             # vitest run
pnpm test:e2e         # playwright
pnpm db:generate      # drizzle-kit generate (schema -> SQL)
pnpm db:migrate       # apply migrations
pnpm db:seed          # load scripts/seed.ts
pnpm db:studio        # drizzle-kit studio
```

Always run `pnpm typecheck && pnpm lint && pnpm test` before declaring a task
complete.

## Database conventions

- **Schema is the source of truth.** Edit `db/schema.ts`, then run
  `pnpm db:generate` to emit SQL. Never hand-edit files in `db/migrations/`.
- **Connection is a singleton.** Import `db` from `db/client.ts`; never call
  `new Database()` elsewhere. SQLite is single-writer — one connection avoids
  `SQLITE_BUSY`.
- **Enable WAL** and a busy timeout in `db/client.ts`:
  ```ts
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 5000");
  db.pragma("foreign_keys = ON");
  ```
- **Every table is tenant-scoped.** Include `tenantId` and index
  `(tenantId, ...)` on any column you filter by. A query without a tenant
  filter is a security bug.
- **Migrations are forward-only.** To change a column, add a new migration;
  never rewrite applied history.
- **Back up before destructive migrations:** `pnpm tsx scripts/backup.ts`.

### Example query

```ts
import { db } from "@/db/client";
import { projects } from "@/db/schema";
import { and, eq } from "drizzle-orm";

export async function listProjects(tenantId: string) {
  return db
    .select()
    .from(projects)
    .where(and(eq(projects.tenantId, tenantId), eq(projects.archived, false)));
}
```

## Auth & authorization

- Server components read the session via `getSession()` from `lib/auth/session`.
- **Never trust client-supplied `tenantId`.** Derive it from the session.
- Route handlers must call `requireUser()` / `requireTenant()` and return the
  guard's response on failure.
- Passwords: Argon2id (`argon2` package). Never log or return password hashes.
- Sessions: random 32-byte tokens stored hashed in the `sessions` table, with
  `expiresAt`; rotate on privilege change.

## Multi-tenancy rules

1. Every row belongs to exactly one tenant.
2. Every read and write includes a tenant predicate — enforce in a shared
   query helper, not ad hoc.
3. Cross-tenant access must be impossible by construction; add a test for any
   new table asserting isolation.
4. Admin/impersonation paths require an explicit audit log entry.

## Environment variables

Copy `.env.example` to `.env.local`. Required:

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | `file:./data/app.db` |
| `SESSION_SECRET` | 32+ byte random string |
| `STRIPE_SECRET_KEY` | billing |
| `STRIPE_WEBHOOK_SECRET` | webhook verification |
| `NEXT_PUBLIC_APP_URL` | absolute URL for links/webhooks |

Validate env at boot with `zod` in `lib/env.ts`; fail fast on missing values.

## Coding standards

- TypeScript strict; no `any` without an inline justification comment.
- Prefer server components; add `"use client"` only when interactivity requires it.
- Data fetching goes through `lib/db/*` — no raw SQL in components.
- All mutations are server actions or route handlers, never client-side writes.
- Errors: use a typed `Result` for expected failures; throw for programmer errors.
- Money is stored as integer cents; never use floats for currency.
- Dates are stored as UTC ISO-8601 strings.

## Testing

- Unit tests colocated as `*.test.ts`.
- Each new query gets a test against an in-memory SQLite database seeded with
  two tenants, asserting isolation.
- E2E covers: signup, login, create resource, billing checkout.
- Never mock the database in integration tests — use a real temp SQLite file.

## Security checklist (run before every PR)

- [ ] No secrets committed; `.env*` is git-ignored.
- [ ] Every query is tenant-scoped.
- [ ] Inputs validated with `zod` at the boundary.
- [ ] Auth guards on every protected route/handler.
- [ ] Stripe webhooks verify signatures before acting.
- [ ] No `dangerouslySetInnerHTML` with user data.
- [ ] Rate limiting on auth and mutation endpoints.

## Common tasks

**Add a table**
1. Define it in `db/schema.ts` with `tenantId`.
2. `pnpm db:generate && pnpm db:migrate`.
3. Add a tenant-scoped query in `lib/db/`.
4. Add an isolation test.

**Add a protected page**
1. Create `app/(app)/<name>/page.tsx`.
2. Call `requireTenant()` at the top.
3. Fetch through `lib/db/`; render with server components.

**Add a webhook**
1. Add a route handler under `app/api/webhooks/`.
2. Verify the signature first; return 400 on mismatch.
3. Make handlers idempotent (dedupe on event id).

## Do not

- Do not open a second SQLite connection.
- Do not write raw SQL inside React components.
- Do not store money as a float.
- Do not trust `tenantId` from the request body or query string.
- Do not edit generated migrations by hand.
- Do not disable the tenant guard "temporarily".
