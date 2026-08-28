# Threadline Console — frontend proof of concept

## What this is

Threadline resolves one person's scattered platform identities — a Slack handle, a
GitHub login, a Teams account — into a single internal record, and generates a
short summary of that person's recent activity from the messages collected across
those platforms. A backend prototype already exists (FastAPI, PostgreSQL +
pgvector, versioned API, scoped authentication). **This project is a standalone
frontend proof of concept** for the console that would sit on top of that backend:
a dashboard for viewing ingested people, their messages, generated summaries, the
health of each platform connector, and the status of the ingestion pipeline itself.

Nothing here talks to a real backend yet. Every piece of data is realistic mock
data, but the mock data layer must be built so it can be swapped for real HTTP
calls to the FastAPI backend later with minimal rework — see "Data layer" below.

## Tech stack (required, not suggestions)

- **Vite + React + TypeScript**
- **shadcn/ui** for every UI primitive (buttons, cards, tables, dialogs, sheets,
  badges, tabs, sidebar, dropdown menus, command palette, toasts). Do not
  introduce a second component library.
- **Tailwind CSS** (comes with shadcn)
- **TanStack Query (v5)** for all data fetching — every resource (people, messages,
  runs, connectors, health) is a query hook; mutations (trigger an ingestion run,
  update a person) go through `useMutation`.
- **TanStack Table (v8)** for every data grid — People, Messages, and Ingestion
  Runs. Sortable columns, column filters, global search, pagination, and column
  visibility toggles are expected, not optional extras.
- **Zustand** for global client state: the mocked auth session, the active
  persona/role, sidebar collapsed state, and theme. Server data belongs in
  TanStack Query, not Zustand — don't duplicate it.
- **React Router v6** for routing, with a protected-route wrapper reading the
  Zustand auth store.
- **lucide-react** for icons (bundled with shadcn).
- **Recharts** (via shadcn's chart components) for the two charts on the
  dashboard.

## Design direction: bright, neutral chrome, one accent, reserved status colors

The goal is a clean, high-contrast, light-mode-first dashboard — not a dark,
muted "hacker terminal" aesthetic. Ship full dark mode too (shadcn's `class`
strategy), but light is the default and the one to get right first.

**Typeface:** Inter (or shadcn's default system-ui stack) for everything. Use
`font-variant-numeric: tabular-nums` on table numeric columns and stat-tile
figures so digits align.

**Core UI tokens** (map these onto shadcn's CSS variables):

| Token | Light | Dark |
|---|---|---|
| `--background` (page) | `#F8FAFC` (slate-50) | `#0B1220` |
| `--foreground` (body text) | `#0F172A` (slate-900) | `#F1F5F9` |
| `--card` | `#FFFFFF` | `#111827` |
| `--card-foreground` | `#0F172A` | `#F1F5F9` |
| `--primary` (brand accent) | `#4F46E5` (indigo-600) | `#818CF8` (indigo-400) |
| `--primary-foreground` | `#FFFFFF` | `#0B1220` |
| `--secondary` | `#F1F5F9` (slate-100) | `#1E293B` |
| `--muted` / `--muted-foreground` | `#F1F5F9` / `#64748B` | `#1E293B` / `#94A3B8` |
| `--accent` (hover/active nav bg) | `#EEF2FF` (indigo-50) | `#1E1B4B` |
| `--border` / `--input` | `#E2E8F0` (slate-200) | `#1E293B` |
| `--ring` | `#4F46E5` | `#818CF8` |
| `--destructive` | `#DC2626` | `#F87171` |

Use `--primary` only for primary CTAs, active nav items, links, and focus rings.
Everything else stays neutral. This restraint is what makes it read as "bright"
rather than "loud."

**Status colors — fixed, reserved, never reused for anything else** (used for
connector status, health checks, run outcomes, filter-category badges' semantic
meaning where relevant). Always pair with an icon and a text label, never color
alone:

| Status | Hex | Use for |
|---|---|---|
| Good / healthy / connected | `#0CA30C` | Connector connected, health check passing, run succeeded |
| Warning / degraded | `#FAB219` | Partial degradation, retrying, nearing a limit |
| Serious | `#EC835A` | Run completed with errors, connector needs attention |
| Critical / down / failed | `#D03B3B` | Health check failing, connector disconnected, run failed |

Because warning/serious sit at low contrast on a white card, render them as a
filled badge (colored background, dark text of the same hue family + icon) rather
than colored text on white — e.g. amber-100 background with amber-800 text and a
`TriangleAlert` icon, not bare amber text.

**Chart categorical palette — fixed order, never remapped.** The four message
filter categories always use these four colors in this order, regardless of which
category has the most volume this week:

| Category | Hex |
|---|---|
| Business | `#2A78D6` (blue) |
| Personal | `#EB6834` (orange) |
| Automated | `#1BAF7A` (teal) |
| Unclear | `#EDA100` (yellow) |

For the messages-over-time chart (a single volume series), use the blue sequential
ramp (`#2A78D6` as the line/bar, a lighter tint of the same hue for any area
fill) rather than introducing a new color.

**Every chart needs:** a visible legend (not color-alone identification), a hover
tooltip, and a "view as table" fallback link for accessibility. Never a dual-axis
chart — if two measures need comparing, use two small charts side by side instead.

## Data model (mock, but shaped like the real API)

Mirror these entities and field names exactly — this is what the real backend
already returns, and matching it now is what makes swapping in real HTTP calls
later a non-event instead of a rewrite.

**Person** — `id, email, full_name, display_name, job_title, timezone, is_active,
created_at, updated_at`

**Connected account** (one platform identity per person) — `id, user_id, platform
("slack" | "github" | "teams" | "email" | "linear"), external_id,
external_handle, external_email, is_primary, created_at`

**Message** — `id, sender_user_id, sender_relation_id, platform,
external_message_id, conversation_id, content, embedding_model, filter_category
("business" | "personal" | "automated" | "unclear"), filter_reason, sent_at`

**Summary** (per person, generated) — `summary, summary_error, generated_at,
message_count, recent_messages[]` (up to 5, verbatim, most recent first)

**Ingestion run** — `run_id, started_at, finished_at, duration_ms, dry_run,
fetched, already_ingested, evaluated, retained, discarded, embedded, persisted,
users_provisioned, filter_provider, embedding_model, filter_errors, status
("success" | "partial" | "failed"), decisions[]` (each: `{id, keep, category,
reason}`)

**Ingestion config** (read-only display) — `filter_system_prompt, llm_provider,
embedding_model, embedding_dim, embedding_executor, embedding_workers`

**System health** — `/health`-style: `{status, version, environment}`; `/ready`-
style: `{status, database: bool, embeddings: bool}`

**Seed enough mock data to make every screen feel real**, not sparse: 15–20
people, 100–150 messages spread realistically across them (mix of all four filter
categories, weighted toward "business"), 6–10 past ingestion runs including at
least one with `filter_errors > 0` and one with `status: "failed"` so the error
states in the UI have something to render.

## Mocked authentication and scopes

A real login screen (email + password fields, "Sign in" button, a bit of brand
polish) that accepts any input, simulates a short network delay, and logs the
user in — no real validation. Also include a **persona switcher** on the login
screen: "Continue as Admin / Analyst / Viewer." This maps onto the backend's real
scope model and should actually gate the UI:

- **Admin** — full access, including triggering ingestion runs and viewing the
  ingestion config.
- **Analyst** — can read people, messages, and summaries; cannot trigger a run
  (the button is visibly disabled with a tooltip explaining why, not hidden).
- **Viewer** — dashboard and status pages only; People/Messages/Runs nav items
  are hidden.

Persist the session in Zustand (localStorage-backed) so a refresh doesn't log the
user out. Include a working "Sign out" in the user menu.

## Pages

Build in this order — each phase should be a coherent, demoable app on its own.

**Phase 1 — shell, auth, dashboard**
- `/login` — described above.
- App shell: collapsible sidebar (shadcn `Sidebar`) with nav to every page below,
  a topbar with global search (shadcn `Command` palette, ⌘K), an environment
  badge ("local"), theme toggle, and user menu.
- `/` (dashboard, protected) — stat tiles (people tracked, messages retained
  today, active connectors, last run status), a system-health strip (three status
  pills: API / Database / Embedding worker), the filter-category breakdown chart,
  the messages-over-time chart, and a compact recent-runs list.

**Phase 2 — People and Messages**
- `/people` — TanStack Table: avatar/initials, name, email, job title, connected
  platforms (small badges with platform icons), message count, last summary date.
  Global search, column filters, pagination.
- `/people/:id` — profile header, connected accounts list, the generated summary
  in a card (with a "Regenerate" button that mock-refreshes it), recent messages
  rendered as a verbatim list.
- `/messages` — TanStack Table: platform icon, sender (links to their person
  page), truncated content with expand, filter-category badge (use the fixed
  categorical colors above), sent_at. Filters by platform, category, and date
  range.

**Phase 3 — Integrations and runs**
- `/integrations` — a card per platform (Slack, GitHub, Teams, Email, Linear)
  showing connection status badge, last sync time, messages contributed, a
  "Configure" button (opens a mock dialog), and "Run ingestion now."
- `/runs` — TanStack Table of past runs with the counters above and a status
  badge; row click opens a detail view with the full per-message decision list.
  "Trigger new run" opens a panel with a simulated step progression (Fetching →
  Deduplicating → Filtering → Embedding → Persisting → Done) before appending a
  new row.
- `/status` — health/readiness cards for API, Database, and Embedding worker
  (status pill + last-checked time), environment and version info, and the
  read-only ingestion config panel.

**Phase 4 — polish**
- `/settings` — mock profile, the persona switcher (also reachable here, not
  just at login), theme preference, and a small "build info" panel.
- Full loading states (skeletons, not spinners, on every table and card),
  empty states (illustration + copy, not a blank table), and error states
  (toast + inline retry) for every query.

## Data layer — build this so it can be swapped, not thrown away

Put every "API call" behind functions in `src/lib/api/` named and shaped exactly
like the real backend's routes (`getUsers`, `getUserSummary`, `getMessages`,
`getIngestionRuns`, `triggerIngestionRun`, `getIngestionConfig`, `getHealth`,
`getReadiness`), each returning the field shapes above. Back them with an
in-memory mock dataset (module-level arrays + simulated latency), ideally through
**MSW (Mock Service Worker)** so the app is making real `fetch` calls that happen
to be intercepted — that makes pointing at the real FastAPI backend later a
one-line base-URL change instead of a rewrite. If MSW adds too much setup
friction, a plain async mock module with artificial delay is an acceptable
fallback — just keep the function signatures and returned shapes identical to
what's described above.

Wrap every one of those functions in a TanStack Query hook (`useUsers()`,
`useUser(id)`, `useMessages(filters)`, `useIngestionRuns()`,
`useTriggerIngestionRun()` as a mutation, etc.) — components should never call
the mock API directly.

## What not to build yet

No real OAuth or SSO, no real backend calls, no real database, no real embedding
model. Everything above is mocked — but mocked to the same shape the real thing
will eventually have.
