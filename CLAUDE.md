# CLAUDE.md

> Opinionated operating manual for a **Next.js 15 (App Router) + SQLite** SaaS.
> Every rule below has a reason. Follow it unless a rule is explicitly overridden
> in `docs/decisions/` (ADRs). When in doubt, match the closest existing pattern
> in the codebase rather than inventing a new one.

---

## 1. Stack & Versions

| Layer        | Choice                                   | Why |
|--------------|------------------------------------------|-----|
| Framework    | **Next.js 15**, App Router, RSC-first    | Server Components cut client JS; App Router is the supported path forward. |
| Language     | **TypeScript**, `strict: true`           | Types are documentation that the compiler enforces. No `any` in app code. |
| Runtime      | **Node.js 20 LTS**                       | Matches Vercel default; stable `fetch`, `structuredClone`, native test runner. |
| Database     | **SQLite** via `better-sqlite3` (local) / **Turso** (edge) | Single-file, zero-ops, synchronous API. Turso for edge deploys. |
| DB access    | **Drizzle ORM** + `drizzle-kit`          | Typed schema, first-class SQLite, generates plain SQL migrations. |
| Auth         | **Auth.js (NextAuth v5)**                | Session in an httpOnly cookie; no client-side token juggling. |
| Styling      | **Tailwind CSS v4** + `shadcn/ui`        | Utility-first, no runtime, copy-in components you own. |
| Validation   | **Zod** at every boundary                | One schema validates forms, API routes, and DB writes. |
| Package mgr  | **pnpm**                                 | Fast, strict, disk-efficient. Lockfile committed. |
| Tests        | **Vitest** (unit) + **Playwright** (e2e)| Fast unit loop; real browser for critical flows. |

**Do not** introduce a second ORM, a second validation library, or a second
styling system. One of each, always.

---

## 2. Folder Structure

```
src/
  app/                      # App Router: routes, layouts, route handlers
    (marketing)/            # route group: public, statically rendered
    (app)/                  # route group: authenticated shell
      dashboard/page.tsx
    api/
      <resource>/route.ts   # Route Handlers ONLY for webhooks + external callers
  components/
    ui/                     # shadcn primitives (generated, do not hand-edit)
    <feature>/              # feature-scoped components
  lib/
    db/
      schema.ts             # Drizzle table definitions (single source of truth)
      client.ts             # the ONE db instance
      migrations/           # generated SQL, committed, never edited by hand
    auth/                   # session helpers, role checks
    <domain>/               # pure business logic, framework-free
  server/
    actions/                # "use server" Server Actions
    queries/                # read functions (typed, reusable)
  types/                    # shared types not owned by a single module
drizzle.config.ts
```

**Rules**
- A file lives in `app/` **only** if it is a route, layout, or route handler.
  Everything else goes in `lib/` or `components/`. `app/` is routing, not logic.
- Server-only code (`db`, secrets) never imports from `components/`. Data flows
  **up** into components as props, not by importing the DB into the client tree.
- One component per file. File name = component name in `kebab-case`.

---

## 3. Naming Conventions

| Thing                | Convention                  | Example |
|----------------------|-----------------------------|---------|
| Files (components)   | `kebab-case.tsx`            | `user-avatar.tsx` |
| Files (logic)        | `kebab-case.ts`             | `calculate-invoice.ts` |
| React components     | `PascalCase`                | `export function UserAvatar()` |
| Hooks                | `use` + `camelCase`         | `useCurrentUser` |
| Server Actions       | verb + noun, `camelCase`    | `createProject`, `deleteTask` |
| DB tables            | `snake_case`, **plural**    | `users`, `invoice_items` |
| DB columns           | `snake_case`                | `created_at`, `user_id` |
| TS types/interfaces | `PascalCase`, no `I` prefix | `type Invoice = {...}` |
| Env vars             | `SCREAMING_SNAKE`           | `DATABASE_URL` |
| Booleans             | `is`/`has`/`should` prefix  | `isActive`, `hasAccess` |

**Why:** a consistent name is a free index. `users` (plural) signals "table of
rows"; `user_id` signals "foreign key" — no comment needed.

---

## 4. SQL & Migration Conventions

- **Schema is code.** Edit `src/lib/db/schema.ts`, then run
  `pnpm drizzle-kit generate` to produce a migration. **Never** write SQL DDL by
  hand and never edit a committed migration.
- **Migrations are append-only and never rewritten.** A bad migration is fixed by
  a *new* migration, not by editing history. Production may have already run it.
- **Every table gets:**
  ```ts
  id: text("id").primaryKey().$defaultFn(() => crypto.randomUUID()),
  createdAt: integer("created_at", { mode: "timestamp" }).notNull().$defaultFn(() => new Date()),
  updatedAt: integer("updated_at", { mode: "timestamp" }).notNull().$defaultFn(() => new Date()),
  ```
  Use `text` UUIDs (portable across SQLite/Turso), integer unix timestamps.
- **Foreign keys are explicit** with an `onDelete` policy. Default to
  `onDelete: "cascade"` for owned children, `"restrict"` for referenced lookups.
  Enable `PRAGMA foreign_keys = ON;` on every connection.
- **Indexes:** add one for every column you filter or join on. SQLite will not
  warn you — it will just get slow. Name them `idx_<table>_<column>`.
- **Enums** are `text` columns with a `CHECK` constraint (or a Zod union),
  not SQLite `CHECK`-less free text.
- **No `SELECT *` in application code.** Select explicit columns so schema
  changes surface as type errors instead of silent data drift.
- **Transactions** wrap any multi-write operation: `db.transaction((tx) => {...})`.
- **Money** is stored as **integer cents** (`amount_cents: integer`), never float.
- **Soft deletes**: prefer a `deleted_at` timestamp over hard deletes for
  user-facing records; filter with a `where(isNull(deletedAt))` helper.

---

## 5. Component Patterns

- **Server Component by default.** Add `"use client"` only when the component
  needs state, effects, event handlers, or browser APIs. Push `"use client"` to
  the leaves of the tree.
- **Data fetching happens in Server Components / Server Actions**, never in
  `useEffect`. Pass data down as props.
- **Mutations use Server Actions**, not client `fetch` to a route handler — unless
  the caller is external (webhook, third-party API), in which case use a Route
  Handler with Zod-validated input.
- **Validate at the boundary, trust inside.** Parse with Zod in the Server Action
  / Route Handler; downstream code receives typed, trusted data.
- **Revalidate after writes:** call `revalidatePath()` / `revalidateTag()` in the
  action so the UI reflects the change without a manual refresh.
- **Loading & error states are mandatory:** each route segment ships
  `loading.tsx` and `error.tsx`. No unstyled white flashes.
- **Compose, don't configure.** Prefer small components + `children` over a
  component with 20 boolean props.

---

## 6. Dev Commands

```bash
pnpm install            # install deps
pnpm dev                # start dev server (http://localhost:3000)
pnpm build              # production build
pnpm start              # run the production build
pnpm lint               # eslint
pnpm typecheck          # tsc --noEmit
pnpm test               # vitest (unit)
pnpm test:e2e           # playwright (e2e)
pnpm db:generate        # drizzle-kit generate  -> new migration
pnpm db:migrate         # apply migrations
pnpm db:studio          # drizzle-kit studio (visual DB browser)
pnpm db:seed            # seed local data
```

**Before every commit:** `pnpm typecheck && pnpm lint && pnpm test`.
CI runs the same three plus `pnpm build`. A red CI is a blocked merge.

---

## 7. What We Don't Do (and Why)

- ❌ **`any`** — it silently disables the compiler. Use `unknown` + Zod parse.
- ❌ **`useEffect` for data fetching** — causes waterfalls and double-fetches.
  Fetch on the server.
- ❌ **Business logic in components** — untestable and duplicated. Put it in
  `lib/<domain>/` as pure functions.
- ❌ **Direct `fetch` to `localhost`/relative API routes from client components**
  for our own data — use Server Actions. Route Handlers are for *external* callers.
- ❌ **Editing generated files** (`components/ui/*`, `migrations/*`) — your change
  is lost on the next generate. Wrap or extend instead.
- ❌ **Floating-point money** — rounding errors compound. Integer cents only.
- ❌ **Secrets in `NEXT_PUBLIC_*`** — anything prefixed `NEXT_PUBLIC_` ships to the
  browser. Server secrets stay unprefixed and are read only in server code.
- ❌ **`SELECT *`** — breaks silently on schema change; select explicit columns.
- ❌ **Committing `.env` / `.db` files** — secrets and local data never enter git.
- ❌ **New dependencies for small utilities** — a 30-line helper beats a 30-package
  transitive tree. Justify every dependency in the PR description.
- ❌ **Premature abstraction** — duplicate twice before extracting. Wrong
  abstractions cost more than duplication.

---

## 8. Definition of Done

A change is done when: types pass, lint passes, unit tests pass, the new path has
a test (or a written reason it can't), migrations are committed, and the PR
description explains the *why*. If a rule above had to be broken, say so and why
in the PR — silent exceptions are how standards rot.
