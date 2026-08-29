# mabinsoft

FastAPI service that ingests messages from third-party platforms, filters them
through an agentic policy, embeds the survivors **locally**, and serves
agent-generated summaries of each person's communication history.

Message bodies are private data: embeddings are generated in-process by a
sentence-transformer, never by a hosted embedding API. (The *reasoning* steps do
call a provider when configured to — see [Privacy boundary](#privacy-boundary).)

> **Prototype, with a production-shaped foundation.** The idea is unproven; the
> scaffolding under it is meant to be the one we keep.
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the conventions guide —
> read it before adding anything. [`docs/PROTOTYPE-REPORT.md`](docs/PROTOTYPE-REPORT.md)
> catalogues every known gap, and [`docs/DOCUMENTATION-BRIEF.md`](docs/DOCUMENTATION-BRIEF.md)
> is the work plan for the full documentation set.

---

## Quick start

```bash
cp .env.example .env          # optional: the compose defaults already work
docker compose up --build
```

```bash
# Trigger one ingestion cycle (this is what the scheduler calls)
curl -s -X POST http://localhost:8000/api/v1/ingestion/runs \
     -H "X-API-Key: local-dev-cron-token" \
     -H "Content-Type: application/json" -d '{}' | jq

# Read users back with their generated summaries
curl -s "http://localhost:8000/api/v1/insights/users?limit=10" \
     -H "X-API-Key: local-dev-reader-token" | jq
```

Interactive docs: <http://localhost:8000/docs> · Version index: `GET /api/versions`

The first build downloads `all-MiniLM-L6-v2` (~90 MB) and bakes it into the
image, so the runtime container needs no network at all.

---

## Layout

```
src/app/
├── main.py                  app factory, lifespan, startup guards
├── api/                     HTTP. Versioned. Knows FastAPI, not SQL.
│   ├── deps.py              composition root
│   ├── errors.py            one error envelope, one place mapping to HTTP
│   ├── system.py            unversioned /health and /ready
│   ├── router.py            version registry + deprecation headers
│   └── v1/{routes,schemas}  this version's endpoints and wire contracts
├── core/                    config, errors, logging, pagination, db/, security/
├── domains/                 identity · messaging · ingestion · insights
└── shared/                  llm/ and embeddings/ — ports with swappable adapters
```

Dependencies point one way — `api → domains → core` — and
`tests/contract/test_layering.py` fails the build if they stop doing so.

Each bounded context has the same five files: `__init__.py`, `models.py`,
`dto.py`, `repository.py`, `service.py`. Routers stay thin, services own
business logic and transaction boundaries, repositories are the only place SQL
is written.

### Data model

| Table | Purpose |
| --- | --- |
| `users` | The internal person: email, name, job title, timezone. |
| `user_relations` | One row per third-party identity (Slack / GitHub / Teams / …) mapped onto one `users.id`. Unique on `(platform, external_id)`. |
| `messages` | Retained text, **two** sender FKs (`sender_user_id` = the resolved person, `sender_relation_id` = the identity it arrived on), the filter verdict and its prompt version, and a `vector(384)` embedding with an HNSW cosine index. |

---

## API

Versions mount under `/api/{version}`. Probes are unversioned infrastructure
contracts and never change shape when the API does.

| Method | Path | Scope required |
| --- | --- | --- |
| GET | `/health` | — |
| GET | `/ready` | — |
| GET | `/api/versions` | — |
| POST | `/api/v1/ingestion/runs` | `ingest:run` |
| GET | `/api/v1/ingestion/config` | `ingest:read` |
| GET | `/api/v1/insights/users` | `insights:read` + `messages:read` |

Every error shares one envelope:

```json
{ "error": { "code": "forbidden", "message": "...", "details": {}, "request_id": "..." } }
```

Who can call what is a table — `AUTH_MATRIX` in
`tests/contract/test_auth_matrix.py` — asserted against the running app, and a
new route without a row fails the suite.

### Domain 1 — ingestion & embedding

`POST /api/v1/ingestion/runs`

1. **Source** — `MockMessageService` returns dummy messages behind the
   `MessageSource` port; a real connector drops in via `get_message_source()`.
2. **Dedupe** — anything already stored under `(platform, external_message_id)`
   is skipped *before* any LLM or CPU work. Safely idempotent, which matters for
   an at-least-once scheduler.
3. **Agentic filter** — batches go to the LLM under
   `INGESTION_FILTER_SYSTEM_PROMPT`. It **fails closed**: an unparseable or
   missing verdict drops the message and is flagged `is_fallback`, so a provider
   outage shows up as `filter_errors` rather than looking like policy.
4. **Identity resolution** — unknown identities are provisioned, keyed on email
   so the same person from two platforms collapses onto one `User`.
5. **Embedding** — dispatched off the event loop (below).
6. **Persist** — one `INSERT … ON CONFLICT DO NOTHING`, in one short
   transaction. `{"dry_run": true}` runs everything and rolls back.

### Domain 2 — retrieval & summarization

`GET /api/v1/insights/users`

Loads a page of users with their relations, fetches each user's recent messages
in **one** windowed query (`row_number() over (partition by sender_user_id)`),
then runs the summarization agent concurrently under a semaphore bounded by
`LLM_MAX_CONCURRENCY`. A failure for one user degrades that entry
(`summary_error`) rather than the page.

---

## Why the embedding work is off the event loop

`SentenceTransformer.encode()` is a synchronous, CPU-bound torch call. Awaiting
it inline would pin the single ASGI event-loop thread for the whole batch and
stall every other in-flight request. `EmbeddingService.embed()` dispatches to a
**dedicated** executor and awaits the future:

- `EMBEDDING_EXECUTOR=thread` (default) — torch releases the GIL inside its
  kernels, so a small thread pool gives real parallelism with one shared copy of
  the model and no IPC.
- `EMBEDDING_EXECUTOR=process` — full GIL isolation, at the cost of one model
  copy per worker and a pickle round-trip.

Deliberately *not* Starlette's `run_in_threadpool`: that pool is shared with
every sync dependency in the app, and saturating it with long embedding batches
would block those too.

---

## Authentication

A `Principal` — subject, kind, scopes, tenant — is resolved at the edge and
passed **into** the service layer, so authorization is a domain concern rather
than a boolean at the door. Routes declare their scopes (which puts them in the
OpenAPI document) and services assert them again (which makes the guarantee
survive the route being reused from a worker).

Scopes name actions: `ingest:run`, `ingest:read`, `insights:read`,
`messages:read`, `admin`.

```bash
API_KEYS='[{"key":"…","subject":"cron-scheduler","scopes":["ingest:run","ingest:read"]}]'
```

`AuthProvider` is a port. Two adapters ship — scoped API keys, and header-based
dev impersonation that production refuses to boot with. **Adding OIDC is one
adapter**: implement `authenticate(request) -> Principal | None`, list it in
`build_auth_chain()`, change nothing else. Static keys have no expiry or
rotation; that limit is tracked as A-2 in the prototype report.

### Privacy boundary

Embeddings are local and always will be. But with `LLM_PROVIDER=anthropic`, the
verbatim text of candidate messages goes to the filtering agent and
8,000-character history transcripts go to the summarization agent. The honest
description is **local embeddings, remote reasoning**. A fully local option is
one `LLMClient` adapter away.

---

## Testing

```bash
make test              # unit + api + contract. No Docker, no network, no model.
make test-integration  # real pgvector via testcontainers
make cov               # coverage report, HTML in htmlcov/
```

Four layers, and three guarantees that keep coverage honest as the code grows:

- **`SOURCE_COVERAGE`** — every module under `src/app` maps to the tests that
  exercise it, or to `Untested("reason")`. Add a file without a decision and the
  suite fails.
- **`AUTH_MATRIX`** — every route × every caller → expected status.
- **`test_layering.py`** — the dependency rules, AST-checked.

[`tests/README.md`](tests/README.md) explains the structure and what is
deliberately untested.

---

## Frontend TypeScript SDK

`custom_generate_unique_id` reduces operation ids to the bare endpoint function
name, so the SDK reads `runIngestion()` and `listUserSummaries()` rather than
`runIngestionApiV1IngestionRunsPost()`. One schema is exported per version, so a
client pinned to v1 cannot call a v2 operation.

```bash
make openapi          # -> openapi/v1.json
npm install && npm run openapi:generate   # -> src/lib/api/v1
```

Copy `api-config.example.ts` into the Next.js app as `src/lib/api-config.ts`.

---

## Demo data

```bash
make seed        # loads the console's fixture dataset
```

Eighteen people, fifty-one external accounts, a hundred and fifty-nine messages
(including commits and twenty-one that belong to nobody yet), seven departments
and nine ingestion runs. It is not invented data: `src/app/seed/fixtures.json`
is generated from the console's own mock database, so the API serves the exact
rows that console was built against.

Safe to run repeatedly - rows are keyed by deterministic ids derived from the
fixture ids, so a second run inserts nothing and a half-seeded database is
completed rather than duplicated. The compose stack sets `SEED_ON_STARTUP=1`,
so `docker compose up` gives you a database with something in it.

Regenerate the fixtures from the console after changing its mock data:

```bash
cd ../ui && bun run scripts/dump-fixtures.ts
```

## Authorization, provisionally

Most of the console-facing surface is unauthenticated while the two platforms
are being wired together. That is a deliberate, temporary state and it is
declared in exactly two places:

- `app/core/security/provisional.py` - every console service method calls
  `require_console_access`, whose body is a no-op today and one edit away from
  enforcing. `ENFORCED = True` closes the whole surface immediately.
- `PROVISIONALLY_OPEN` in `tests/contract/test_auth_matrix.py` - the list of
  route names, each with a reason, plus a test asserting every one of them
  really is reachable without credentials. A new route must still be declared
  somewhere, and the declaration says which state it is in.

The ingestion routes are **not** in that list: they keep their scopes.

## Local development

```bash
make install                  # uv sync --group dev --group integration
docker compose up -d db       # Postgres + pgvector only
cp .env.example .env
make migrate
make run
```

`make help` lists every target. `make check` is exactly what CI runs on a pull
request.

---

## Configuration

Every setting is an environment variable; `validate_for_environment()` refuses
to start on a configuration that is unsafe rather than merely wrong. See
`.env.example` for the annotated list.

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | — | Must use `postgresql+asyncpg://` |
| `API_KEYS` | — | JSON array of `{key, subject, scopes}`. Falls back to `CRON_TOKEN` |
| `DEV_AUTH_ENABLED` | `true` | Header impersonation. Fatal in production |
| `DOCS_ENABLED` | `true` | Forced off in production |
| `LLM_PROVIDER` | `stub` | `stub` (offline heuristic) or `anthropic` |
| `INGESTION_FILTER_SYSTEM_PROMPT` | business-only | The filtering agent's whole policy |
| `PROMPT_VERSION` | `v1` | Stored on every message the filter decides on |
| `EMBEDDING_DIM` | `384` | Must match the model **and** the migration |
| `EMBEDDING_EXECUTOR` | `thread` | `thread` or `process` |
| `LLM_MAX_CONCURRENCY` | `4` | Ceiling on parallel summarization calls |

---

## Production notes

- Non-root container, no build tooling in the runtime image.
- Migrations run from the entrypoint (`RUN_MIGRATIONS=0` when you move to a
  dedicated migration job — required with more than one replica).
- `/health` is liveness (no I/O); `/ready` touches the database and the
  embedding pool and returns **503** when degraded.
- Logs are JSON with a propagated `X-Request-Id`.
