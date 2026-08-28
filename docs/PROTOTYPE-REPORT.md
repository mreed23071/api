# threadline — Prototype Technical Report

**Status:** prototype / bootstrap scaffold
**Reviewed:** 2026-08-25
**Commit state:** initial scaffold, never executed against a live database
**Audience:** engineers, and the agent that will write the platform's high-level documentation

---

## 0. How to read this document

This is a **descriptive audit of what the code currently does**, plus a
prioritized list of what it does not yet do. It deliberately makes no claims
about the platform's business goals, target market, or roadmap — those are not
inferable from the source, and §10 lists the questions a human needs to answer
before anyone writes that part.

Where this document says *"recommended"*, that is an engineering opinion, not an
existing decision. Everything else is fact, traceable to a file path.

Two caveats on provenance:

- The code has **never been run**. Package registries were unreachable from the
  build environment, so no dependency was installed, no container was built, no
  migration applied, no test executed. Verification was static: full-syntax
  compile, an AST pass confirming every intra-project import resolves to a real
  symbol, and a runtime import-cycle check. Behaviour described below is
  behaviour *as written*, not behaviour *as observed*.
- All 12 messages in the system come from a hardcoded fixture. There is no real
  data and no real connector.

---

## 0.1 Update — foundation refactor (2026-08-25)

This report was written against the original scaffold. A follow-up refactor
hardened the foundation: versioned API package, a real principal-based auth
model, a tenancy seam, unit-of-work transactions, one error envelope, structured
logging, and a four-layer test suite. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the conventions that resulted.

**Sections 4-6 of this report still describe the data model and behaviour
accurately. Section 5's auth column and section 8's file paths are superseded**
— routes now live under `src/app/api/v1/routes/`, and `GET /api/v1/insights/users`
requires `insights:read` + `messages:read`.

Findings closed:

| | Finding | How |
| --- | --- | --- |
| A-1 | Unauthenticated insights endpoint | Requires `insights:read` + `messages:read`, asserted at the route *and* in the service |
| A-3 | Default credential works in production | `Settings.validate_for_environment()` refuses to boot |
| A-6 | Public docs everywhere | Disabled in production by the same guard |
| A-7 | Invalid `WWW-Authenticate` challenge | Emits the real scheme names from the auth chain |
| A-8 | Wildcard CORS with credentials | Startup guard rejects it in every environment |
| R-3 | One transaction across all LLM/CPU work | Reads and slow work happen outside; one short write transaction |
| R-6 | `/ready` returned 200 when degraded | Returns 503 |
| R-8 | Dedupe ignored platform in SQL | Matches the full composite key |
| R-9 | `EMBEDDING_DIM` had two sources of truth | `test_the_embedding_dimension_matches_the_migration` |
| R-10 | `model` read off the client with `getattr` | Declared on the `LLMClient` protocol |
| S-5 | Readiness ran a model forward pass per probe | Checks pool state instead |
| O-2 | Unstructured, uncorrelated logs | JSON logs, request-id middleware, `run_id` in scope during a run |
| T-1 | No integration tests | `tests/integration/` against real pgvector via testcontainers |
| T-2 | No CI | `.github/workflows/ci.yml` |
| T-3 | Builds not reproducible | `uv.lock` is committed and both `uv sync` calls in the Dockerfile use `--frozen` |
| T-4 | No SDK drift check | `scripts/export_openapi.py --check`, run in CI |

Partially addressed:

| | Finding | What changed, what remains |
| --- | --- | --- |
| A-2 | Shared secret is the whole auth model | A `Principal` with scopes now reaches the service layer and `AuthProvider` is a port — but the shipped adapter is still static keys with no expiry or rotation. OIDC is one adapter away |
| A-4 | No authorization model | `TenantContext` threads through every repository via `scoped()`; no `organization_id` column yet |
| P-3 | Unversioned agent policy | `filter_prompt_version` is stored on every message; prompts are still plain env vars, not versioned rows |
| R-2 | Silent data loss on provider outage | `filter_errors` and a per-decision `is_fallback` flag make it visible in the run report and the logs; nothing alerts on it yet |

Untouched, and still the shape of the next milestone: **R-1** (synchronous
ingestion), **R-4/R-5** (no run history, rejected messages re-evaluated forever),
**S-1** (vectors never read), **S-2** (summaries uncached), **P-1/P-2** (remote
reasoning, no erasure path), **O-1** (no metrics).

---

## 1. What the prototype is

A FastAPI service that:

1. **Ingests** messages from third-party communication platforms (Slack,
   GitHub, Teams, email, Linear are modelled; none are implemented).
2. **Filters** them through an LLM whose policy is a configurable system prompt
   — shipped default: *"Filter out personal messages and retain only
   business-related messages."*
3. **Embeds** the survivors with a locally-hosted sentence-transformer
   (`all-MiniLM-L6-v2`, 384 dimensions) and stores the vectors in PostgreSQL via
   pgvector.
4. **Resolves identity** — a Slack handle, a GitHub login and a Teams UPN all
   map onto one internal `User`, so one person has one history.
5. **Summarizes** each person's communication history through a second LLM
   agent and serves users alongside those summaries.

The stated privacy position is that **message bodies never leave the network for
embedding**. That guarantee holds for embeddings and only for embeddings — see
finding **P-1**.

### 1.1 What is real vs. what is a placeholder

| Component | State |
| --- | --- |
| FastAPI app, routing, DI, config, lifespan | Real |
| PostgreSQL + pgvector schema and migration | Real, unapplied |
| Async SQLAlchemy 2.0 repositories and queries | Real |
| Local embedding pipeline + executor offload | Real |
| Docker image, compose stack, entrypoint | Real, never built |
| OpenAPI → TypeScript SDK toolchain | Real config, never generated |
| **Message source connectors** | **Mock only** (`MockMessageService`, 12 fixed messages) |
| **LLM** | **Stub by default** — a keyword heuristic, not a model. Anthropic adapter exists but is opt-in |
| **Authentication** | **One shared secret on two routes; none on the third** |
| **Semantic search** | **Absent** — vectors are written, never read |
| Tests | 2 files, 6 tests, unit-only |

---

## 2. Runtime topology

`docker-compose.yaml` defines two services:

| Service | Image | Role |
| --- | --- | --- |
| `db` | `pgvector/pgvector:pg17` | PostgreSQL 17 with the `vector` extension available. Named volume `postgres-data`. Health-gated with `pg_isready`. |
| `api` | built from `./Dockerfile` | The FastAPI app. Depends on `db` being healthy. Exposes 8000. |

`DATABASE_URL` is composed inside `docker-compose.yaml` from the same
`POSTGRES_*` variables the database uses, with host `db` — so credentials stay
on the private compose network and are never baked into the image.

### 2.1 Image build (`Dockerfile`)

Two stages.

- **builder** — `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`. Runs
  `uv sync --no-install-project --no-dev` against `pyproject.toml`, then
  **downloads the sentence-transformer weights into `/opt/huggingface`** at
  build time, then installs the project itself.
- **runtime** — plain `python:3.12-slim-bookworm`. Copies `/app/.venv` and the
  baked model cache, runs as non-root uid 1001, sets `HF_HUB_OFFLINE=1` and
  `TRANSFORMERS_OFFLINE=1`.

The runtime container therefore needs no network access at all when
`LLM_PROVIDER=stub`.

`pyproject.toml` pins torch to the `pytorch-cpu` index on Linux, avoiding the
~2.5 GB CUDA stack that the default PyPI wheel would pull in.

### 2.2 Startup sequence

1. `scripts/entrypoint.sh` runs `alembic upgrade head` unless `RUN_MIGRATIONS=0`.
2. `exec uvicorn app.main:app`.
3. `create_app()` builds the FastAPI instance, mounts CORS, registers
   `/health` and `/ready`, includes the versioned router at `/api/v1`, and calls
   `assert_unique_operation_ids(app)` — which raises at construction time if two
   endpoint functions would generate the same SDK method name.
4. The `lifespan` context configures logging, **starts the embedding executor**
   and **warms the model** (`EMBEDDING_WARMUP_ON_STARTUP=true`), then constructs
   the LLM client.
5. On shutdown: close the LLM transport, shut the executor down, dispose the
   SQLAlchemy engine.

The HEALTHCHECK gives a 40-second start period, which is the budget for the
model warm-up.

---

## 3. Configuration surface

Every setting is an environment variable, read once into a cached
`Settings` singleton (`src/app/core/config.py`). `DATABASE_URL` is validated at
startup to require the `postgresql+asyncpg://` scheme.

| Variable | Default | Effect |
| --- | --- | --- |
| `DATABASE_URL` | local DSN | Async DSN. Rejected at startup if not `postgresql+asyncpg://` |
| `APP_ENV` | `local` | `local` / `staging` / `production`. Only behavioural effect: production refuses to fall back to the stub LLM |
| `LOG_LEVEL` | `INFO` | Root log level |
| `API_PREFIX` | `/api/v1` | Mount point for all domain routes |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Browser origins allowed. Used with `allow_credentials=True` |
| `CRON_TOKEN` | `local-dev-cron-token` | Shared secret for `X-Cron-Token`. **The default is a working credential** |
| `EMBEDDING_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` | Baked into the image at build time |
| `EMBEDDING_DIM` | `384` | Must match both the model and the migration |
| `EMBEDDING_BATCH_SIZE` | `32` | Encoder batch size |
| `EMBEDDING_EXECUTOR` | `thread` | `thread` or `process` |
| `EMBEDDING_WORKERS` | `1` | Executor pool size |
| `EMBEDDING_TORCH_THREADS` | `2` | `torch.set_num_threads` ceiling per worker |
| `EMBEDDING_WARMUP_ON_STARTUP` | `true` | Load the model during lifespan |
| `LLM_PROVIDER` | `stub` | `stub` or `anthropic` |
| `ANTHROPIC_API_KEY` | empty | Required when provider is `anthropic` |
| `LLM_MODEL` | `claude-sonnet-4-5` | Model id passed to the provider |
| `LLM_MAX_TOKENS` | `1024` | Per-call ceiling |
| `LLM_TIMEOUT_SECONDS` | `30` | Provider timeout |
| `LLM_MAX_CONCURRENCY` | `4` | Semaphore bound on parallel summarization calls |
| `INGESTION_FILTER_SYSTEM_PROMPT` | business-only policy | The filtering agent's entire policy |
| `SUMMARY_SYSTEM_PROMPT` | 3-sentence brief | The summarization agent's entire brief |
| `RUN_MIGRATIONS` | `1` | Entrypoint applies migrations before serving |

**Note for the doc author:** both agent policies are runtime configuration, not
code. Changing what counts as a "business message" is an env-var change and a
restart. That is a deliberate design property worth stating in the platform
docs — and a governance problem, see finding **P-3**.

---

## 4. Data model

Three tables, created by `migrations/versions/0001_initial_schema.py`. A
PostgreSQL enum `platform` carries `slack | github | teams | email | linear |
other`. All primary keys are UUIDs with `gen_random_uuid()` server defaults; all
tables carry server-side `created_at` / `updated_at`.

### 4.1 `users`

The internal person, independent of any platform.

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid PK | |
| `email` | varchar(320) | **unique**, indexed — the identity-merge key |
| `full_name` | varchar(255) | not null |
| `display_name`, `job_title`, `timezone` | nullable | |
| `is_active` | bool | default true; drives the `active_only` filter |

### 4.2 `user_relations`

One row per third-party identity, mapped onto one `users.id`. **This is the
table that makes the platform's core claim work** — that one person's Slack,
GitHub and Teams activity is one history.

| Column | Type | Notes |
| --- | --- | --- |
| `user_id` | uuid FK → `users.id` | `ON DELETE CASCADE` |
| `platform` | enum | |
| `external_id` | varchar(255) | The platform's stable id |
| `external_handle`, `external_email` | nullable | |
| `is_primary` | bool | Declared, **never read by any code path** |
| `details` | jsonb | Provider-specific payload |

Constraints: `UNIQUE (platform, external_id)` — an external identity belongs to
exactly one internal user. Indexes on `user_id` and `(user_id, platform)`.

### 4.3 `messages`

| Column | Type | Notes |
| --- | --- | --- |
| `sender_user_id` | uuid FK → `users.id` | `ON DELETE CASCADE`. The resolved person; summaries group by this |
| `sender_relation_id` | uuid FK → `user_relations.id` | `ON DELETE SET NULL`. The identity it actually arrived on — provenance survives identity merges |
| `platform` | enum | |
| `external_message_id` | varchar(255) | |
| `conversation_id` | varchar(255) | indexed |
| `content` | text | Verbatim message body |
| `embedding` | `vector(384)` | Nullable. L2-normalised |
| `embedding_model` | varchar(255) | Which model produced the vector |
| `filter_category` | varchar(64) | `business` / `personal` / `automated` / `unclear` |
| `filter_reason` | text | The agent's justification — an audit trail for prompt tuning |
| `sent_at` | timestamptz | indexed |
| `source_metadata` | jsonb | |

Constraints and indexes:

- `UNIQUE (platform, external_message_id)` — **the idempotency key** for an
  at-least-once scheduler.
- `(sender_user_id, sent_at)` composite — serves the summarization query.
- `ix_messages_embedding_hnsw` — HNSW, `vector_cosine_ops`, `m=16`,
  `ef_construction=64`. Cosine is correct because the encoder normalises.
  **Nothing in the codebase queries this index.**

### 4.4 Two sender foreign keys — why

Worth spelling out in the platform docs, because it is the least obvious design
decision in the schema. `sender_user_id` answers *"whose history is this?"* and
survives re-mapping. `sender_relation_id` answers *"which account did this
actually come from?"* and is nulled rather than cascaded, so merging two
identities into one user does not destroy the record of where a message came
from.

---

## 5. API reference

Base URL in local compose: `http://localhost:8000`. Versioned routes are under
`/api/v1`. Interactive docs at `/docs`, schema at `/openapi.json` — **both
unauthenticated in every environment**.

### 5.0 Route summary

| Method | Path | Operation ID | Auth | Tag |
| --- | --- | --- | --- | --- |
| GET | `/health` | `get_health` | **none** | system |
| GET | `/ready` | `get_readiness` | **none** | system |
| POST | `/api/v1/ingestion/runs` | `run_ingestion` | `X-Cron-Token` | ingestion |
| GET | `/api/v1/ingestion/config` | `get_ingestion_config` | `X-Cron-Token` | ingestion |
| GET | `/api/v1/insights/users` | `list_user_summaries` | **none** | insights |

Five routes. Operation IDs are the bare Python function names — see §7.

---

### 5.1 `GET /health` — liveness

**Auth:** none. **Source:** `src/app/main.py`

Pure in-process response, no I/O. Used by the container HEALTHCHECK.

```json
{ "status": "ok", "version": "0.1.0", "environment": "local" }
```

Always 200 if the process is alive.

---

### 5.2 `GET /ready` — readiness

**Auth:** none. **Source:** `src/app/main.py`

Actively probes both dependencies:

1. Opens a session and runs `SELECT 1`.
2. Calls `EmbeddingService.embed_one("ready")` — **a real forward pass through
   the model on every call**.

```json
{ "status": "ok", "database": true, "embeddings": true }
```

`status` is `"degraded"` if either check fails. Note it **still returns HTTP
200** when degraded — an orchestrator checking status codes alone will consider
a broken pod ready. See finding **R-6**.

---

### 5.3 `POST /api/v1/ingestion/runs` — run one ingestion cycle

**Operation ID:** `run_ingestion` → SDK `runIngestion()`
**Auth:** `X-Cron-Token: <shared secret>`, constant-time compared
**Source:** `src/app/domains/ingestion/router.py`, `service.py`

The cron-triggered entry point. Fully synchronous: the HTTP response is not
returned until source fetch, filtering, embedding and persistence have all
completed.

**Request body** (optional — `{}` or omitted is valid):

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `limit` | int 1–1000, nullable | null | Cap on messages pulled from the source |
| `system_prompt_override` | string, nullable | null | Replaces `INGESTION_FILTER_SYSTEM_PROMPT` **for this run only** |
| `dry_run` | bool | false | Runs the entire pipeline, then rolls the transaction back |

**Response 200** — a machine-readable run report:

| Field | Meaning |
| --- | --- |
| `run_id` | UUID generated per run. **Not persisted anywhere** |
| `started_at`, `finished_at`, `duration_ms` | Timing |
| `dry_run` | Echo of the request |
| `fetched` | Messages the connector returned |
| `already_ingested` | Skipped as duplicates before any LLM or CPU work |
| `evaluated` | Sent to the filtering agent |
| `retained` | Classified as keep |
| `discarded` | Classified as drop |
| `embedded` | Vectors generated |
| `persisted` | Rows written (always 0 on a dry run) |
| `users_provisioned` | Previously-unknown identities auto-created |
| `filter_provider` | `stub` or `anthropic` |
| `embedding_model` | Model that produced the vectors |
| `decisions[]` | Per-message `{id, keep, category, reason}` |

**Failure modes:**

- `401` — missing or wrong token.
- `422` — body fails validation.
- An LLM failure does **not** fail the request. The filter agent catches it,
  marks the whole batch `keep=false, category=unclear, reason="agent error: …"`
  and the run reports them as discarded. **Silent data loss under a provider
  outage** — mitigated only by the fact that unstored messages are re-evaluated
  on the next run. See finding **R-2**.
- A database or embedding failure propagates as an unhandled 500; there is no
  exception handler registered.

---

### 5.4 `GET /api/v1/ingestion/config` — inspect active pipeline configuration

**Operation ID:** `get_ingestion_config` → SDK `getIngestionConfig()`
**Auth:** `X-Cron-Token`

Read-only introspection of the knobs the pipeline is running with:
`filter_system_prompt`, `llm_provider`, `embedding_model`, `embedding_dim`,
`embedding_executor`, `embedding_workers`.

Useful for verifying a deploy picked up a prompt change. Note this **returns the
full system prompt** — it is behind the cron token, which is the right instinct,
but the token is a machine credential, not an operator identity.

---

### 5.5 `GET /api/v1/insights/users` — users with generated summaries

**Operation ID:** `list_user_summaries` → SDK `listUserSummaries()`
**Auth:** ❗ **NONE**
**Source:** `src/app/domains/insights/router.py`, `service.py`

**Query parameters:**

| Param | Type | Default | Bounds |
| --- | --- | --- | --- |
| `limit` | int | 20 | 1–100 |
| `offset` | int | 0 | ≥ 0 |
| `active_only` | bool | true | |
| `messages_per_user` | int | 25 | 1–200 |

**Response 200:**

```
{
  items: [{
    user: { id, email, full_name, display_name, job_title, timezone, is_active,
            created_at, updated_at },
    relations: [{ id, user_id, platform, external_id, external_handle,
                  external_email, is_primary, details, created_at }],
    message_count: int,
    summary: string | null,
    summary_error: string | null,
    generated_at: datetime | null,
    recent_messages: [{ id, platform, content, sent_at }]   // up to 5, verbatim
  }],
  total, limit, offset, llm_provider, llm_model
}
```

**This single unauthenticated endpoint returns:** every user's email address,
real name and job title; every linked third-party account handle across every
platform; the verbatim text of their five most recent retained messages; and an
LLM-generated behavioural summary of their communication history. It is the most
sensitive object the system can produce, and it is served to anyone who can
reach the port. See finding **A-1**.

**Behaviour worth documenting:**

- Summaries are generated **on every request**. There is no cache. A page of 20
  users is 20 LLM calls, bounded to `LLM_MAX_CONCURRENCY=4` at a time.
- A user with zero messages short-circuits to the literal string
  `"No retained messages for this user yet."` with no LLM call.
- A per-user LLM failure sets `summary_error` and leaves `summary` null; the
  rest of the page still returns. One user's failure never fails the page.
- The transcript sent to the model is capped at 8,000 characters, keeping the
  most recent tail.

---

## 6. Request lifecycles

### 6.1 Ingestion (Domain 1), step by step

`IngestionService.run()` — `src/app/domains/ingestion/service.py`

1. **Fetch.** `MockMessageService.fetch(limit)` returns up to 12 hardcoded
   `RawMessage` objects across Slack, GitHub and Teams, from three fictional
   authors, deliberately mixing business and personal content so the filter has
   something to do. It implements the `MessageSource` protocol; a real connector
   is a one-line change in `get_message_source()`.
2. **Dedupe.** Queries `messages` for the external ids in this batch and drops
   anything already stored. This runs *before* filtering and embedding, so
   duplicates cost one indexed query rather than an LLM call and a forward pass.
3. **Filter.** `MessageFilterAgent` batches messages 20 at a time, sends each
   batch as a JSON envelope under the configured system prompt, and parses a
   strict `{"decisions": [...]}` reply. The parser tolerates markdown fences and
   chatty preambles. It **fails closed**: an unparseable reply, or a message the
   model returned no verdict for, is dropped.
4. **Resolve identity.** For each retained message, look up
   `(platform, external_author_id)` in `user_relations`. Unknown identities are
   **auto-provisioned**: find-or-create a `User` keyed on the author's email
   (falling back to `<external_id>@unknown.local`), then create the
   `UserRelation`. This is what makes the same person arriving from Slack and
   GitHub collapse onto one user.
5. **Embed.** One `EmbeddingService.embed()` call for all retained bodies,
   dispatched to the executor (§8.2).
6. **Persist.** A single `INSERT … ON CONFLICT DO NOTHING` against the
   idempotency constraint, returning the ids actually written.
7. **Commit or roll back** depending on `dry_run`, then build the run report.

Everything from step 2 to step 7 happens inside **one database transaction that
stays open across every LLM call and the whole embedding batch** — see finding
**R-3**.

### 6.2 Insights (Domain 2), step by step

`UserInsightsService.list_with_summaries()`

1. Load a page of users with `selectinload(User.relations)` — one query for
   users, one for relations, no cartesian product.
2. `count_users()` for the pagination total (a second query).
3. `MessageRepository.latest_for_users()` — **one** query using
   `row_number() OVER (PARTITION BY sender_user_id ORDER BY sent_at DESC)` to
   pull every user's most recent N messages together. This is the deliberate
   avoidance of the N+1 that would otherwise make the endpoint collapse.
4. For each user concurrently, under an `asyncio.Semaphore(LLM_MAX_CONCURRENCY)`:
   render a transcript (oldest-first, truncated to 8k chars, prefixed with the
   user's name and email) and call the summarization agent.
5. Assemble and return.

All ORM relationships are declared `lazy="raise"`, so an accidental lazy load —
which in async SQLAlchemy would raise a confusing `MissingGreenlet` at runtime —
instead fails loudly and immediately at development time.

---

## 7. Frontend SDK contract

FastAPI's default operation id is `"{function}_{path}_{method}"`, which
generates TypeScript like `runIngestionApiV1IngestionRunsPost()`.
`custom_generate_unique_id` in `src/app/core/openapi.py` returns the bare
endpoint function name instead, so the generated SDK reads `runIngestion()` and
`listUserSummaries()`.

The trade-off: the function name becomes the *only* thing keeping operation ids
unique. `assert_unique_operation_ids(app)` runs during `create_app()` and raises
on a collision, so the failure surfaces at boot rather than as a route silently
vanishing from the generated client.

Toolchain:

```
npm run openapi:export     # scripts/export_openapi.py  ->  openapi.json
npm run openapi:generate   # openapi-ts.config.ts       ->  src/lib/api
```

`openapi-ts.config.ts` configures `@hey-api/openapi-ts` with the native fetch
client (`@hey-api/client-fetch`), flat functions rather than a class,
`throwOnError: true`, and a `runtimeConfigPath` pointing at a file the frontend
supplies — `api-config.example.ts` is the template, carrying base URL and
headers.

`scripts/export_openapi.py` imports the app without opening a database
connection or loading the model, so it is safe to run in CI with nothing else up.

---

## 8. Internal components worth knowing about

### 8.1 The LLM is a port, not a dependency

`src/app/shared/llm/base.py` defines an `LLMClient` protocol with one method:
`complete(LLMRequest) -> LLMResponse`. Two adapters ship:

- **`stub`** (default) — `src/app/shared/llm/stub.py`. A keyword heuristic over
  two curated word lists (`BUSINESS_MARKERS`, `PERSONAL_MARKERS`). It is not a
  model. It exists so the entire pipeline runs end to end in CI and on a laptop
  with no credentials and no egress, and so tests are deterministic.
- **`anthropic`** — the Messages API, with `max_retries=2` and a configurable
  timeout. In non-production it warns and falls back to the stub if the key is
  missing; in production it refuses to start.

`LLMRequest` carries an `LLMTask` discriminator (`CLASSIFY` / `SUMMARIZE`). Real
providers ignore it; the stub uses it to pick a strategy.

**Consequence for the platform docs:** with default configuration the "agentic
filtering" and "agentic summarization" the platform advertises are performed by
a keyword matcher. That is a deliberate, defensible default for a prototype, but
it should not be described as AI behaviour without the qualifier.

### 8.2 Why embedding work is pushed off the event loop

`SentenceTransformer.encode()` is a synchronous, CPU-bound torch call. Awaiting
it inline would pin the single ASGI event-loop thread for the entire batch and
stall every other in-flight request.

`EmbeddingService.embed()` therefore dispatches to a **dedicated** executor via
`loop.run_in_executor` and awaits the future:

- `EMBEDDING_EXECUTOR=thread` (default) — torch releases the GIL inside its
  kernels, so a small thread pool gives real parallelism with one shared copy of
  the model and no IPC.
- `EMBEDDING_EXECUTOR=process` — a `ProcessPoolExecutor` for full GIL isolation,
  at the cost of one model copy per worker and a pickle round-trip.

The pool is deliberately **not** Starlette's `run_in_threadpool`: that pool is
shared with every sync dependency and `def` endpoint in the app, and saturating
it with long embedding batches would block those too. The worker code lives in
its own module with module-level functions so it is picklable by the process
pool, and the model is loaded once per worker through the executor
`initializer`.

`EmbeddingService.embed()` also asserts that the model's output width matches
`EMBEDDING_DIM`, so a model swap fails loudly instead of writing vectors the
column cannot hold.

### 8.3 Repositories

The only place SQL is written. Two patterns worth noting:

- `MessageRepository.latest_for_users()` uses a window function to fetch every
  user's recent messages in one round trip (§6.2).
- `MessageRepository.bulk_upsert()` is a single
  `INSERT … ON CONFLICT DO NOTHING … RETURNING id` against the idempotency
  constraint, which is what makes the cron endpoint safe to retry.

Services own transaction boundaries; the `get_session` dependency only
guarantees rollback-and-close.

---

## 9. Gap analysis

Findings are grouped by theme and tagged with a severity. **Recommendations are
engineering opinions**, not decisions that have been made.

### 9.0 The five to fix first

| | Finding | Why first |
| --- | --- | --- |
| 1 | **A-1** — `/api/v1/insights/users` is unauthenticated | It serves names, emails, job titles, cross-platform handles, verbatim message text and behavioural summaries to anyone who can reach the port |
| 2 | **A-2 / A-4** — no identity or authorization model | A shared secret gates *a route*; nothing anywhere knows *who* is calling or what they may see |
| 3 | **R-1 / R-3** — ingestion is one long synchronous request in one long transaction | Fine for 12 fixture messages; the first real connector breaks it |
| 4 | **T-1 / T-2** — nothing has ever been executed, no CI | Every claim in this document is static analysis, including the ones about correctness |
| 5 | **S-1** — vectors are written but never read | The most expensive thing the system does currently has no consumer |

---

### 9.1 Authentication & authorization

**A-1 — `GET /api/v1/insights/users` has no authentication whatsoever.** ⛔ Critical
The router is declared without a `dependencies=[...]` guard, unlike the ingestion
router. It returns PII, third-party account mappings, verbatim message bodies and
LLM-generated behavioural profiles.
*Recommendation:* gate it behind real user authentication before it is exposed on
any network, and scope results to what the caller is permitted to see (A-4).
Short term, if it must ship first, put it behind the same token as ingestion —
that is not adequate, but it is not nothing.

**A-2 — A single static shared secret is the entire auth model.** ⛔ Critical
`X-Cron-Token` is compared in constant time (correct), but it has no identity, no
expiry, no rotation mechanism, no scope, no revocation and no audit trail. It is
one value shared by every caller, distributed through env vars, and equally
valid forever.
*Recommendation:* separate the two concerns the token is currently conflating.
For the scheduler, an OIDC service-account credential (workload identity from the
platform the cron runs on) validated as a short-lived JWT with an `ingest:run`
scope. For human and frontend callers, a session-based user identity — OIDC/OAuth
against the company IdP — carried as a bearer token. Keep a scoped, hashed,
rotatable API-key table only if third-party machine callers are actually a
requirement. Whatever the mechanism, the result must be a *principal object* that
reaches the service layer, not a boolean at the door.

**A-3 — The default `CRON_TOKEN` is a working credential.** 🔴 High
`local-dev-cron-token` is the default in `config.py`, `.env.example` and
`docker-compose.yaml`. A deployment that forgets to set it is authenticated by a
value published in the repository. `APP_ENV=production` does not check it.
*Recommendation:* refuse to start in production when any secret still holds its
default, alongside the existing production check on `ANTHROPIC_API_KEY`.

**A-4 — There is no authorization model at all.** 🔴 High
No tenant, no organization, no team, no per-user visibility rule. Every query is
global. There is nothing in the schema that could express "user X may read user
Y's summary."
*Recommendation:* decide the tenancy model before the schema hardens — this is
the single hardest thing to retrofit. If threadline is multi-tenant, an
`organization_id` on `users`, `user_relations` and `messages`, enforced in the
repository layer (or by Postgres row-level security), needs to land now rather
than after the first customer.

**A-5 — No rate limiting.** 🟡 Medium
Both expensive endpoints are unthrottled. `POST /ingestion/runs` burns CPU on
embeddings; `GET /insights/users?limit=100` triggers up to 100 LLM calls per
request. The latter is an unauthenticated cost-amplification vector today.
*Recommendation:* per-principal rate limits at the ingress, plus an application
guard that refuses concurrent ingestion runs.

**A-6 — `/docs` and `/openapi.json` are public in every environment.** 🟡 Medium
The schema advertises the ingestion routes and the token header name.
*Recommendation:* disable interactive docs when `APP_ENV=production`, or put them
behind the same authentication as everything else.

**A-7 — `WWW-Authenticate: X-Cron-Token` is not a valid challenge.** 🔵 Low
The header expects an auth *scheme*. Cosmetic, but it will confuse a generated
client or a strict proxy.

**A-8 — CORS runs with `allow_credentials=True` and operator-supplied origins.** 🔵 Low
Correct as configured, but nothing prevents someone setting `CORS_ORIGINS=["*"]`,
which combined with credentials is both invalid per spec and dangerous.
*Recommendation:* validate at startup that the origin list contains no wildcard
whenever credentials are enabled.

---

### 9.2 Privacy & data governance

**P-1 — The privacy guarantee is narrower than it sounds.** 🔴 High
"Embeddings never leave the network" is true and enforced. But when
`LLM_PROVIDER=anthropic`, **the verbatim text of every candidate message** goes
to the filtering agent, and **8,000-character transcripts of a person's message
history** go to the summarization agent, over the public internet.
*Recommendation:* state the boundary explicitly in the platform docs — local
embeddings, remote reasoning — and treat a fully local reasoning option
(self-hosted model behind the same `LLMClient` port) as a roadmap item if the
privacy positioning is load-bearing commercially. The port already makes this a
single-adapter change.

**P-2 — No deletion path and no retention policy.** 🔴 High
There is no way to delete a person's messages, embeddings or summaries. FK
cascades would remove messages if a `User` row were deleted, but nothing exposes
that, and there is no soft-delete, no retention window, and no tombstone for
"this person asked to be forgotten."
*Recommendation:* a retention policy per platform, a hard-delete path for a
subject-erasure request, and a decision on whether embeddings count as personal
data in the relevant jurisdictions (they are derived from message content and
are not anonymised — the safe assumption is yes).

**P-3 — The agent policy is unversioned runtime configuration.** 🔴 High
`INGESTION_FILTER_SYSTEM_PROMPT` decides what is retained, and
`system_prompt_override` lets a single request use a different policy. Neither
the prompt text nor its version is stored with the verdict. After a prompt
change, no one can explain why a given message was kept — `filter_reason` records
the justification but not the policy that produced it.
*Recommendation:* store prompts as versioned rows, persist
`filter_prompt_version` on `messages`, and treat a prompt change as a migration
event with a documented decision. Consider removing `system_prompt_override`
from the public contract, or restricting it to a dry-run-only debugging affordance
— today it lets any holder of the cron token silently change retention policy for
a run.

**P-4 — Auto-provisioning trusts unverified upstream identity claims.** 🟡 Medium
`_resolve_identities()` creates a `User` keyed on `author_email` as supplied by
the connector. A connector that reports an attacker-controlled email would attach
that identity — and therefore its messages — to an existing person's history.
*Recommendation:* only auto-merge on emails from a verified domain the connector
can attest to; otherwise create an unlinked identity and require an explicit
merge (which does not exist yet — G-2).

**P-5 — No redaction before LLM calls.** 🟡 Medium
Message bodies go to the provider unmodified: credentials pasted into Slack,
customer data, secrets in PR comments.
*Recommendation:* a redaction pass (secrets, card numbers, keys) before any
outbound LLM call, applied in the agent layer so both agents inherit it.

**P-6 — Synthetic `@unknown.local` emails pollute the identity key.** 🔵 Low
Every identity with no email gets `<external_id>@unknown.local`, which is the
merge key. Two different people with no email on two platforms will not merge —
correct — but the value looks like a real address in the API response.
*Recommendation:* make `users.email` nullable and merge on a verified-email
match only.

---

### 9.3 Correctness & reliability

**R-1 — Ingestion is one long synchronous HTTP request.** 🔴 High
Twelve fixture messages return quickly. A real connector returning thousands
will run source fetch, N LLM round trips and a full embedding batch inside a
single request, past any reverse-proxy or cron client timeout, with no
checkpoint and no partial progress.
*Recommendation:* make `POST /ingestion/runs` enqueue a job and return `202` with
a run id, and add `GET /ingestion/runs/{id}` for status. A queue (Celery, arq,
or Postgres-backed) also gives retries, concurrency control and a natural home
for the run history R-4 wants.

**R-2 — An LLM outage silently discards a whole batch.** 🔴 High
`MessageFilterAgent` fails closed: on an unparseable or failed response every
message in the batch is marked `keep=false, category=unclear`. The run reports
them as *discarded*, indistinguishable in the counters from a legitimate policy
rejection. Fail-closed is the right default for a privacy filter; the invisibility
is the problem. The saving grace is that discarded messages are never stored, so
the next run re-evaluates them — but nothing alerts anyone in the meantime.
*Recommendation:* count agent errors separately in the run report, log them at
error level with the run id, and alert when the error rate crosses a threshold.

**R-3 — One database transaction is held open across all LLM and CPU work.** 🔴 High
The session opens at request start and commits at the very end, spanning the
dedupe query, every filtering round trip, identity provisioning writes and the
entire embedding batch. A connection is pinned for the whole run, and provisioned
rows are locked throughout.
*Recommendation:* split into short transactions — read the dedupe set, close;
do the LLM and CPU work outside a transaction; open a write transaction only for
provisioning and the final insert.

**R-4 — Run reports are ephemeral.** 🟡 Medium
`run_id` is generated per run and returned in the response body, then discarded.
There is no `ingestion_runs` table, so there is no history, no "when did we last
succeed", and nothing to alert on.
*Recommendation:* persist a run record with counters, status, prompt version and
error detail. It is also the natural place to hang the async job status from R-1.

**R-5 — Rejected messages are re-fetched and re-classified forever.** 🟡 Medium
Dedupe checks only *stored* messages. Anything the filter rejects is never
stored, so every subsequent run pulls it again and pays for another
classification. With a real source, the cost of each run grows with the total
history of rejected messages, without bound.
*Recommendation:* a lightweight ledger of evaluated-and-rejected external ids
(id, platform, decided_at, prompt version), consulted during dedupe. It doubles
as the audit trail for what was filtered out and why.

**R-6 — `/ready` returns HTTP 200 when degraded.** 🟡 Medium
The body says `"status": "degraded"`, but an orchestrator reading status codes —
which is the normal configuration — will route traffic to a pod with no database.
*Recommendation:* return 503 when either check fails.

**R-7 — Retry behaviour is inconsistent and undocumented.** 🟡 Medium
The Anthropic adapter sets `max_retries=2`; nothing else retries anything. There
is no backoff policy for database connection failures, and a failed run has no
automatic recovery beyond the next cron tick.
*Recommendation:* make the retry policy explicit and uniform at the port level
rather than a per-adapter constructor argument.

**R-8 — `existing_external_ids()` filters on `external_message_id` only.** 🔵 Low
Platform is compared in Python after the rows come back, not in the `WHERE`
clause. Results are correct; the query over-fetches when two platforms use
overlapping id formats.

**R-9 — `EMBEDDING_DIM` has two sources of truth.** 🔵 Low
384 is set in `Settings` and hardcoded again as a constant in the migration.
`EmbeddingService` catches a mismatch at runtime, which is the important guard,
but the duplication invites drift.

**R-10 — `llm_model` is read off the client with `getattr(..., "unknown")`.** 🔵 Low
The `LLMClient` protocol does not declare a `model` attribute, so the response
field is best-effort.
*Recommendation:* put `model` on the protocol.

**R-11 — `is_primary` is declared and never used.** 🔵 Low
The column exists on `user_relations` but no code reads or sets it, and there is
no operation to designate a primary identity or to merge or split identities.

**R-12 — Migrations run on every container start.** 🟡 Medium
`entrypoint.sh` runs `alembic upgrade head` before serving. With more than one
replica, replicas race on the same migration.
*Recommendation:* keep the entrypoint behaviour for local development
(`RUN_MIGRATIONS` already toggles it) and move to a dedicated migration job or
init container in any orchestrated environment.

---

### 9.4 Scale & performance

**S-1 — Vectors are written but never read.** 🔴 High
`messages.embedding` is populated on every ingestion, and an HNSW cosine index is
built and maintained on it, at real cost in CPU and write amplification. **No
code path queries it.** There is no semantic search endpoint, and the
summarization agent uses raw text, not vectors.
*Recommendation:* either ship the retrieval endpoint that justifies the column
(nearest-neighbour search over a person's or a team's history, or a
retrieval-augmented summary that scales past the 8k-character transcript cap), or
drop the column until there is a consumer. Right now it is the most expensive
unused feature in the system.

**S-2 — Summaries are recomputed on every request.** 🟡 Medium
No cache, no persistence, no invalidation key. Two identical requests cost twice
and can return different text. A dashboard polling this endpoint re-bills every
poll.
*Recommendation:* persist summaries with the inputs that produced them
(message count, latest `sent_at`, prompt version, model) and regenerate only when
that key changes. This also gives the summary history G-6 wants.

**S-3 — One embedding worker by default.** 🟡 Medium
`EMBEDDING_WORKERS=1` with `EMBEDDING_TORCH_THREADS=2` is a safe default for a
small container, and a hard throughput ceiling for a real ingestion volume.
*Recommendation:* size it against measured throughput once a real connector
exists; the knob is already there.

**S-4 — Offset pagination plus a `COUNT(*)` per page.** 🟡 Medium
`count_users()` runs a second query on every request, and offset pagination
degrades as the table grows.
*Recommendation:* keyset (cursor) pagination, and either cache the total or drop
it from the envelope.

**S-5 — `/ready` runs a model forward pass on every probe.** 🔵 Low
A 15-second readiness probe means a permanent background CPU load and a
permanently occupied executor slot.
*Recommendation:* check that the executor is alive and the model is loaded,
rather than encoding a string.

**S-6 — Pool sizes are tuned independently and can starve each other.** 🔵 Low
`db_pool_size=5` and `LLM_MAX_CONCURRENCY=4` are unrelated numbers; ingestion
holds a connection for the duration of its LLM work (R-3), so under concurrency
the pool exhausts before the LLM semaphore does.

---

### 9.5 Observability

**O-1 — No metrics.** 🔴 High
Nothing measures ingestion duration, filter keep-rate, LLM error rate or latency,
embedding queue depth, or summary generation cost. The run report is the only
quantitative output, and it is thrown away (R-4).
*Recommendation:* Prometheus or OpenTelemetry metrics on those five things
first — the keep-rate in particular is the signal that tells you a prompt change
broke retention.

**O-2 — Logs are unstructured and uncorrelated.** 🟡 Medium
Plain stdlib formatting to stdout. No request id, no trace id. `run_id` is
attached to a `LoggerAdapter` inside the ingestion service but does not reach the
formatter, so it never appears in output.
*Recommendation:* structured JSON logs with a request-id middleware, and make
`run_id` a first-class log field.

**O-3 — No error tracking.** 🟡 Medium
An unhandled 500 surfaces only in stdout. No exception handlers are registered.
*Recommendation:* Sentry or equivalent, plus a handler that converts unhandled
exceptions into a consistent error envelope so the generated SDK has one shape to
type against.

**O-4 — No distributed tracing.** 🔵 Low
Worth adding once ingestion becomes asynchronous (R-1) and a request spans a
queue.

---

### 9.6 Testing, CI and reproducibility

**T-1 — Nothing has ever been executed.** 🔴 High
Six unit tests exist across two files, covering the filter agent's protocol
handling and the OpenAPI operation-id contract. There is **no** test that touches
Postgres, pgvector, the migration, the repositories, the ingestion service, the
insights service, or the real embedding model. The window-function query, the
`ON CONFLICT` upsert, the pgvector asyncpg codec registration and the HNSW index
creation are all unverified.
*Recommendation:* integration tests against a real `pgvector/pgvector` container
(testcontainers or a compose service in CI), covering at minimum: migration
up/down, the upsert idempotency guarantee, `latest_for_users()`, and one
end-to-end ingestion run with the stub LLM.

**T-2 — No CI pipeline.** 🔴 High
No workflow file. Nothing runs ruff, mypy, pytest or the OpenAPI export on a
change.

**T-3 — `uv.lock` is not committed.** 🟡 Medium
The Dockerfile is written to accept a lockfile (`uv.loc[k]` glob) but none
exists, so `uv sync` resolves fresh on every build and image contents are not
reproducible.
*Recommendation:* `uv lock`, commit it, add `--frozen` to both sync calls.

**T-4 — No drift check between the API and the generated SDK.** 🟡 Medium
`openapi.json` is gitignored and generated on demand. Nothing fails when a
backend change breaks the frontend's client.
*Recommendation:* generate the schema in CI and fail if it differs from the
committed copy.

---

### 9.7 Functional gaps

These are absences rather than defects — the things a reader of the platform
documentation might reasonably expect to exist.

| | Gap |
| --- | --- |
| **G-1** | No real connectors. `MockMessageService` returns 12 hardcoded messages. There is also no concept of an incremental cursor or watermark, which every real connector needs |
| **G-2** | No CRUD for users or identities. They can only come into existence through auto-provisioning during ingestion. No merge, no split, no unlink |
| **G-3** | No semantic search endpoint, despite the vector column and index (S-1) |
| **G-4** | No way to read raw messages. `recent_messages` (5 per user, inside the summaries response) is the only path to message content |
| **G-5** | No multi-tenancy (A-4) |
| **G-6** | Summaries are never persisted, so there is no history and no way to see how someone's communication profile changed |
| **G-7** | The TypeScript SDK config exists but no frontend consumes it; `api-config.example.ts` is a template |
| **G-8** | No operational surface — no way to replay a run, inspect why a specific message was filtered, or re-run summarization for one user |

---

### 9.8 Deployment & operations

**D-1 — No graceful drain.** 🟡 Medium
`EmbeddingService.shutdown()` calls `shutdown(wait=True, cancel_futures=True)`.
Queued-but-unstarted embedding work is cancelled on shutdown; combined with the
synchronous ingestion request (R-1), a deploy mid-run loses that run's progress.

**D-2 — The baked model is pinned by name, not revision.** 🔵 Low
`sentence-transformers/all-MiniLM-L6-v2` without a revision hash means two builds
on different days can bake different weights, silently changing every embedding
produced afterwards — with no migration path for vectors already in the table.
*Recommendation:* pin the revision, and record `embedding_model` with the
revision (the column exists) so a re-embedding campaign can find stale rows.

**D-3 — No backup or restore story.** 🟡 Medium
A named Docker volume is the entire durability plan.

**D-4 — Secrets are plain environment variables.** 🟡 Medium
Appropriate for local compose; needs a secret manager and injected short-lived
credentials in any real environment.

---

## 10. For the documentation author

### 10.1 Things to get right

- The system's distinguishing idea is **cross-platform identity resolution**:
  `user_relations` is what lets one person's Slack, GitHub and Teams activity be
  summarized as a single history. Everything else is machinery around that.
- The **privacy story is "local embeddings, remote reasoning"** — precise
  language matters here (P-1). Do not write "your messages never leave your
  network" without qualification.
- With default configuration, the **"agentic" steps are a keyword heuristic**,
  not a model (§8.1). Describing the *capability* is fair; describing current
  default *behaviour* as AI is not.
- **Nothing has been run.** Do not describe performance characteristics,
  throughput or latency; none have been measured.
- The **vector column has no consumer yet** (S-1). Semantic search is a
  plausible-sounding feature that does not exist.

### 10.2 Open questions only a human can answer

The source cannot tell you any of this, and guessing will produce documentation
that has to be retracted:

1. **What is threadline for, commercially?** Team analytics, knowledge retention,
   compliance and eDiscovery, manager tooling, onboarding context — the schema
   supports several readings and they imply very different products.
2. **Who is the user?** The only read endpoint returns *other people's*
   summaries, which suggests a manager or analyst persona rather than an
   individual contributor viewing their own history. Is that intended?
3. **Is this single-tenant or multi-tenant?** Determines whether A-4 is a
   blocking schema change (it probably is).
4. **What is the consent model?** People whose messages are ingested and
   profiled are not the people using the product. Whether they consent, are
   notified, or can opt out is a product decision with legal weight.
5. **Which platform lands first**, and does that connector need real-time
   events or is polling acceptable?
6. **Is the privacy positioning commercial or incidental?** If customers are
   buying "your data stays private", the remote LLM calls (P-1) need to change.
7. **What is the intended retention period**, and what happens on an erasure
   request (P-2)?
8. **Is the filter policy per-deployment, per-tenant or per-user?** Today it is
   one global env var.

### 10.3 Glossary

| Term | Meaning in this codebase |
| --- | --- |
| **Bounded context** | A directory under `src/app/domains/` owning its own models, schemas, repository, service and router |
| **Identity resolution** | Mapping a third-party account (`user_relations`) onto an internal `User`, so one person has one history |
| **Auto-provisioning** | Creating a `User` and `UserRelation` on the fly during ingestion for an identity never seen before |
| **Filtering agent** | The LLM step that decides whether a message is retained, governed by `INGESTION_FILTER_SYSTEM_PROMPT` |
| **Summarization agent** | The LLM step that turns a person's recent messages into a few sentences |
| **Retained / discarded** | A message the filtering agent kept (stored) or rejected (never stored) |
| **Run** | One invocation of `POST /ingestion/runs`; reported but not persisted |
| **Dry run** | A full pipeline execution that is rolled back instead of committed |
| **Stub provider** | The default offline keyword heuristic standing in for an LLM |
| **Port / adapter** | `LLMClient` and `MessageSource` are protocols; concrete implementations are swappable via dependency injection |
| **Executor offload** | Running CPU-bound embedding work in a thread or process pool so the ASGI event loop stays responsive |

### 10.4 File map

| Path | Contains |
| --- | --- |
| `src/app/main.py` | App factory, lifespan, `/health`, `/ready`, CORS |
| `src/app/api.py` | Mounts both domain routers under `/api/v1` |
| `src/app/core/config.py` | Every environment variable, as a typed `Settings` |
| `src/app/core/db.py` | Async engine, session dependency, `Base`, pgvector codec registration |
| `src/app/core/security.py` | The cron-token guard — the entire auth layer |
| `src/app/core/openapi.py` | Operation-id override and the uniqueness assertion |
| `src/app/domains/identity/` | `User`, `UserRelation`, their schemas and repositories |
| `src/app/domains/messaging/` | `Message`, the vector column, the message repository |
| `src/app/domains/ingestion/` | Domain 1: router, service, mock source, filtering agent |
| `src/app/domains/insights/` | Domain 2: router, service, summarization agent |
| `src/app/shared/llm/` | `LLMClient` port, stub and Anthropic adapters, factory |
| `src/app/shared/embeddings/` | Executor-backed encoder and its picklable worker |
| `migrations/versions/0001_initial_schema.py` | The whole schema, including the HNSW index |
| `openapi-ts.config.ts`, `scripts/export_openapi.py` | TypeScript SDK generation |
| `docker-compose.yaml`, `Dockerfile`, `scripts/entrypoint.sh` | The runtime |
