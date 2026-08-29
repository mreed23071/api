# Documentation brief — mabinsoft

**For:** the agent (or engineer) writing the platform documentation
**Input:** the `mabinsoft` repository, in full
**Output:** a `docs/` tree of markdown, committed alongside the code
**Status of the subject:** working prototype with a deliberately
production-shaped foundation. Nothing has run in production; no connector is
real; no performance has been measured.

---

## 1. Your task

Produce the documentation set defined in §5 — a layered `docs/` tree that
explains mabinsoft to two audiences at once:

- a **business reader** who needs to understand what it does, why the design
  choices matter commercially, and what it would take to grow it into a
  platform;
- an **engineer** who needs to extend it on Monday morning without reading every
  file first.

Work through the phases in order. Each document in §5 names its audience, what
it must cover, where the truth for it lives in the repository, and how you know
it is done.

### How to work

1. **Read the existing documentation first**, in this order:
   `README.md` → `docs/ARCHITECTURE.md` → `docs/PROTOTYPE-REPORT.md` →
   `tests/README.md`. Between them they already contain most of the accurate
   technical content; a large part of your job is reorganising, deepening and
   framing it for the two audiences rather than rediscovering it.
2. **Verify every claim against the source.** Cite the file path where a reader
   can check you (`src/app/core/security/principal.py`), and prefer naming the
   function or test that proves a behaviour over asserting it.
3. **Do not run the application** — you will not be able to. It is verified by
   static analysis only; see §3.
4. When the repository and this brief disagree, **the repository wins**. Say so
   in your summary.

---

## 2. Non-negotiable ground rules

These exist because the failure modes they prevent have already been hit once in
this project's short life.

**Separate fact from recommendation.** Everything descriptive must be traceable
to code. Where you propose something — a queue, an IdP, a metrics stack — label
it as a recommendation and give the trade-off, not just the conclusion.

**Do not invent the business.** The source cannot tell you what mabinsoft is
commercially *for*. `docs/PROTOTYPE-REPORT.md` §10.2 lists eight open questions
(purpose, persona, tenancy, consent model, first connector, whether the privacy
position is commercial, retention, whether the filter policy is global). Carry
them forward into an explicit **"Open product questions"** page, phrased as
questions. Do not resolve them by guessing; documentation that has to be
retracted is worse than documentation that admits a gap.

**Be precise about the privacy claim.** Embeddings are generated locally and
never leave the network. But with `LLM_PROVIDER=anthropic`, verbatim message
text goes to the filtering agent and 8,000-character history transcripts go to
the summarization agent. The honest phrase is **"local embeddings, remote
reasoning."** Never write "your messages never leave your network" unqualified.

**Be precise about what is real.** With default configuration the "agentic"
filtering and summarization are performed by a **keyword heuristic**
(`src/app/shared/llm/stub.py`), not a model. Describing the *capability* is fair;
describing current default *behaviour* as AI is not. Likewise the only message
source is a 12-message fixture.

**Never state performance.** No throughput, latency or cost figure has been
measured. If a number would help, write the method for measuring it instead.

**Personal data is the subject matter.** `GET /api/v1/insights/users` returns
names, email addresses, cross-platform account handles, verbatim message
excerpts and a behavioural summary of a person who is not the user. Treat that
as the sensitive thing it is throughout — especially in the business-facing
pages.

---

## 3. Seed facts (verified — you may rely on these)

Use this to calibrate; still confirm against the code as you write each page.

**Stack.** Python 3.12, FastAPI, async SQLAlchemy 2.0 + asyncpg, PostgreSQL 17
with pgvector, Alembic, `uv` for dependency management, Docker + Compose,
`@hey-api/openapi-ts` for the TypeScript client.

**Routes.** Five, plus a version index:

| Method | Path | Scopes |
| --- | --- | --- |
| GET | `/health` | none — liveness, no I/O |
| GET | `/ready` | none — 503 when degraded |
| GET | `/api/versions` | none — version index |
| POST | `/api/v1/ingestion/runs` | `ingest:run` |
| GET | `/api/v1/ingestion/config` | `ingest:read` |
| GET | `/api/v1/insights/users` | `insights:read` + `messages:read` |

**Scopes.** `ingest:run`, `ingest:read`, `insights:read`, `messages:read`,
`admin`. They name *actions*, not roles.

**Tables.** `users`, `user_relations` (the cross-platform identity map, unique on
`(platform, external_id)`), `messages` (two sender FKs, a `vector(384)` column,
an HNSW cosine index, the filter verdict and its prompt version).

**Layering.** `api → domains → core`, enforced by AST checks in
`tests/contract/test_layering.py`.

**Test suite.** Four layers — `unit/`, `api/`, `contract/`, `integration/` — with
28 test files. Three "coverage-at-a-glance" mechanisms: `SOURCE_COVERAGE`,
`AUTH_MATRIX`, and the layering rules.

**Verification status.** The code has never been executed by its author:
package registries were unreachable. It was verified by full-syntax compile, an
AST pass confirming every intra-project import resolves to a real symbol, a
runtime import-cycle check, and the same layering/coverage rules the contract
tests encode. State this plainly wherever it matters — it is a fact about the
codebase's maturity, not a disclaimer to bury.

---

## 4. The narrative arc

The documentation should tell one story across its pages:

> **A prototype with a production-shaped foundation.** The idea — resolving one
> person's scattered platform identities into a single, summarised communication
> history — is unproven. The scaffolding beneath it was built as though it were
> already the real product, so that proving the idea does not mean throwing the
> code away.

Three themes recur, and each page should reinforce whichever applies:

1. **Seams over features.** The expensive changes were made cheap *in advance*:
   a `Principal` that already reaches the service layer (so OIDC is one adapter),
   a `TenantContext` threaded through every repository (so multi-tenancy is one
   method body), ports for the LLM, the embedder and the message source (so each
   vendor is a swap). Explain each seam as *the retrofit it avoids*.
2. **Enforcement over convention.** Rules that are only written down decay. The
   architecture is asserted by tests — layering, auth coverage, structural
   coverage, OpenAPI drift. Whenever you document a rule, name the test that
   enforces it.
3. **Honest boundaries.** Local embeddings but remote reasoning; fail-closed
   filtering that is now *visible* rather than silent; a vector index with no
   consumer yet. The documentation's credibility comes from naming these, not
   from smoothing them over.

---

## 5. Deliverables

Target tree (adjust if you find a better shape, and say why):

```
docs/
├── README.md                          index and reading paths
├── product/
│   ├── overview.md                    what it does, for whom, why it matters
│   ├── concepts.md                    the domain vocabulary
│   ├── privacy-and-data.md            what is collected, sent where, retained
│   └── open-questions.md              what only a human can decide
├── architecture/
│   ├── overview.md                    layers, contexts, request lifecycles
│   ├── data-model.md                  tables, relationships, why two sender FKs
│   ├── authentication.md              principals, providers, the OIDC path
│   ├── authorization-and-scopes.md    scopes, enforcement, tenancy seam
│   ├── api-versioning.md              versions, operation ids, deprecation
│   ├── error-contract.md              the envelope and its codes
│   ├── ingestion-pipeline.md          domain 1, end to end
│   ├── insights-pipeline.md           domain 2, end to end
│   ├── embeddings-and-concurrency.md  local model, executor offload
│   ├── llm-integration.md             the port, adapters, prompts as config
│   ├── persistence.md                 sessions, unit of work, repositories
│   └── observability.md               logging, correlation, what is missing
├── platform/
│   ├── growth-path.md                 prototype → platform, staged
│   ├── tooling.md                     the toolchain and what each buys
│   ├── typescript-sdk.md              schema → client, end to end
│   ├── deployment.md                  container, compose, config, guards
│   └── scaling-considerations.md      known limits and the fixes
├── engineering/
│   ├── getting-started.md             zero to a running stack
│   ├── conventions.md                 how to add a route/context/version
│   ├── testing-strategy.md            the four layers and what belongs where
│   ├── database-learning-path.md      guided study for the DB stack
│   └── contributing.md                workflow, review expectations
└── reference/
    ├── configuration.md               every environment variable
    ├── api-reference.md               generated + narrated
    └── glossary.md
```

### Phase A — Orientation

**`docs/README.md`** · *both audiences*
The entry point. One-paragraph description, a diagram of the whole system, and
**three reading paths**: "I'm evaluating this" (product/), "I'm going to build
on it" (engineering/getting-started → architecture/overview → conventions),
"I need to make a decision about it" (platform/growth-path,
product/open-questions). Done when a newcomer knows where to go in 30 seconds.

**`product/overview.md`** · *business*
What the system does in plain language; the problem cross-platform identity
resolution solves; who the software is for (marking the persona question as
open); what exists today versus what is aspiration. Must contain an explicit
**"What is real today"** table — real / mocked / absent. Done when a
non-engineer can explain the product and would not be surprised by a demo.

**`product/concepts.md`** · *both*
The domain vocabulary as a business reader would meet it: person vs identity,
retention policy, ingestion run, dry run, retained/discarded, summary,
embedding, semantic similarity. Explain *embedding* without linear algebra.
Source: `docs/PROTOTYPE-REPORT.md` §10.3, expanded.

**`product/privacy-and-data.md`** · *business, legally literate*
The single most important business page. What data enters the system, what is
stored, what leaves the network and under which configuration, what is derived
(embeddings and summaries are derived personal data), who can read it, and what
does not exist yet (retention policy, erasure path — findings P-1, P-2).
Done when someone could take this into a security review and not be embarrassed.

**`product/open-questions.md`** · *business*
The eight questions from `PROTOTYPE-REPORT.md` §10.2, each with why it matters,
what it blocks, and roughly when it must be answered. Multi-tenancy is the one
with a deadline: it is the hardest to retrofit.

### Phase B — Architecture and feature areas

Each of these is a **feature area**. Every one follows the same shape:
*what problem it solves → how it works → why it was built this way → what
enforces it → what it does not do yet → where to change it.*

**`architecture/overview.md`** · *engineer*
The layer diagram and the dependency rule; the four bounded contexts and why
`identity`/`messaging` have no HTTP surface while `ingestion`/`insights` do; the
standard five-file shape of a context; both request lifecycles end to end. Name
the enforcing test for each rule.

**`architecture/data-model.md`** · *both*
The three tables, their constraints and indexes, and the reasoning: why `email`
is the merge key and why that is currently a weakness (P-4); **why `messages`
has two sender foreign keys** (the least obvious decision in the schema — one
answers "whose history is this", the other "which account did this arrive on",
and it survives identity merges); why the vector column exists and why nothing
reads it yet (S-1). Include an ERD.

**`architecture/authentication.md`** · *engineer, with a business summary*
The `Principal` object and why it is passed *into* the service layer rather than
checked at the door. `PrincipalKind`, `TenantContext`, `auth_scheme`. The
`AuthProvider` port and its two adapters: scoped API keys (constant-time
comparison against every record, so timing reveals nothing) and header-based dev
impersonation that production refuses to boot with. Why 401 and 403 are kept
distinct. **A concrete "adding OIDC" walkthrough** — the one class and one line —
because that is the page's real payoff. State the known limits of static keys
honestly (A-2: no expiry, no rotation, no revocation).

**`architecture/authorization-and-scopes.md`** · *engineer, with a business summary*
Why scopes name actions rather than roles, and where roles therefore live. The
five scopes and the capability each represents. **Double enforcement** — routes
declare (so it appears in OpenAPI), services assert (so the guarantee survives
the service being called from a worker) — and why both. `AUTH_MATRIX` as the
readable answer to "who can do what", plus the completeness check that stops it
drifting. Then the **tenancy seam**: `TenantContext`, `Repository.scoped()`, the
four-step path to multi-tenancy, and the test that makes step four true.

**`architecture/api-versioning.md`** · *engineer, with a business summary*
Path scheme and the `API_VERSIONS` registry; per-version OpenAPI documents and
why a client pinned to v1 cannot call v2; version headers and RFC-8594/9745
deprecation signalling; the **operation-id trade-off** stated with both options
and why bare function names were chosen; what counts as breaking versus additive;
a step-by-step "shipping v2". The business framing: versioning is how you change
the product without breaking the customers already on it.

**`architecture/error-contract.md`** · *engineer*
The envelope, the `ErrorCode` values as a public contract clients branch on, the
one place mapping domain errors to HTTP, why unhandled 500s are deliberately
opaque, and how `request_id` correlates a user's complaint to a log line.

**`architecture/ingestion-pipeline.md`** · *both*
The six stages. Emphasise: dedupe runs *before* any LLM or CPU work; the
idempotency key that makes an at-least-once scheduler safe; **fail-closed
filtering** and why privacy beats recall there, plus how `filter_errors` and
`is_fallback` make an outage visible rather than looking like policy; the
transaction shape (slow work outside, one short write transaction) and what it
prevents. Close with the known limits: synchronous (R-1), no run history (R-4),
rejected messages re-evaluated forever (R-5).

**`architecture/insights-pipeline.md`** · *both*
Page load, the single windowed query that avoids the N+1, bounded concurrent
summarization, per-user degradation instead of page failure. Limits: recomputed
every request (S-2), transcript truncated at 8k characters where retrieval over
the embeddings we already store is the real fix.

**`architecture/embeddings-and-concurrency.md`** · *engineer*
The best standalone teaching page in the codebase. Why a synchronous CPU-bound
torch call in a coroutine stalls every other in-flight request; `run_in_executor`
and the dedicated pool; thread versus process and when each is right; why
Starlette's shared threadpool is deliberately *not* used; the model loaded once
per worker via the executor initializer; the dimension guard. Should leave a
reader able to reason about CPU-bound work in any async service.

**`architecture/llm-integration.md`** · *both*
The port; the stub as the default (offline, deterministic, no credentials) and
what that means for what "agentic" currently denotes; the Anthropic adapter and
the production guard that refuses to silently degrade; **prompts as
configuration** — the operational power and the governance problem (P-3), with
`filter_prompt_version` as the partial answer.

**`architecture/persistence.md`** · *engineer*
Async session lifecycle, why `get_session` never commits, the unit of work and
why transaction boundaries are explicit and short, savepoints for composition,
the repository pattern and the one-hook tenancy rule, `lazy="raise"` and why a
loud failure beats a `MissingGreenlet` in production.

**`architecture/observability.md`** · *engineer*
Structured JSON logs; contextvar correlation so no call site threads ids around;
`X-Request-Id` propagation from the ingress; `run_id` during ingestion. Then the
gap — no metrics (O-1) — with the five to add first and why filter keep-rate is
the one that tells you a prompt change broke retention.

### Phase C — Platform growth and tooling

**`platform/growth-path.md`** · *business and technical leadership*
The centrepiece. Prototype → platform in **staged milestones**, each with: what
it unlocks, what must be built, what it depends on, and roughly how hard it is.
A defensible ordering, with reasoning:

1. **Prove the idea** — one real connector, an incremental cursor, real users.
2. **Make ingestion operable** — queued jobs, run history, alerting on
   `filter_errors`. Blocks any real volume.
3. **Answer the tenancy question** — the schema decision that gets more
   expensive every week it waits.
4. **Real identity** — OIDC, per-user authorization, an audit trail.
5. **Make the vectors earn their keep** — semantic search, retrieval-augmented
   summaries that scale past the transcript cap.
6. **Data governance** — retention, erasure, prompt versioning as data.
7. **Scale and cost** — summary caching, connector fan-out, metrics-driven
   tuning.

For each, say what in the current design already anticipates it. That is the
argument for the foundation, made concretely rather than asserted.

**`platform/tooling.md`** · *engineer and technical leadership*
Every tool, what problem it solves, and what breaks without it. Cover: **Docker
and Compose** (one command to a working stack; the model baked at build time so
the runtime container needs no network; non-root; healthchecks), **uv** (fast
resolution, `uv.lock` committed and `--frozen` builds — reproducible images),
**Alembic** (schema as reviewable code; why autogenerated migrations are always
read by a human), **pgvector**, **@hey-api/openapi-ts**, **testcontainers**,
**ruff and mypy**, **the Makefile** as the single definition of every command CI
runs, **GitHub Actions**. Then the tooling a growing platform will need and does
not have: a job queue, a metrics stack, a secret manager, an IdP, error
tracking, a connector SDK or template, feature flags, an evaluation harness for
agent quality. For each, the signal that says "now".

**`platform/typescript-sdk.md`** · *engineer, frontend-facing*
The full loop: FastAPI route → `custom_generate_unique_id` → per-version
`openapi/v1.json` → `@hey-api/openapi-ts` → typed client → Next.js. Why
operation ids were overridden, with the before/after method names. The drift
check in CI and what it prevents — a backend change silently breaking the
frontend's types. How a frontend consumes the error envelope as one narrowable
type. How to pin a version.

**`platform/deployment.md`** · *engineer*
The two-stage image; the entrypoint and why migrations run there today and must
move to a dedicated job at more than one replica (R-12); liveness versus
readiness; configuration and the **startup guards** — enumerate each and the
silent failure it prevents; what production still needs (secret manager,
backups, ingress rate limiting).

**`platform/scaling-considerations.md`** · *engineer*
Where this design bends and where it breaks: synchronous ingestion, one
embedding worker, offset pagination, uncached summaries, pool sizing between the
DB and the LLM semaphore. Each with the symptom you would see first.

### Phase D — Engineering practice

**`engineering/getting-started.md`** · *engineer*
Zero to a running stack, then zero to a passing test suite, then a first change
end to end (add a field, regenerate the schema, watch the contract tests react).
Include what to expect on the first `docker compose up` — the model download —
so nobody thinks it hung.

**`engineering/conventions.md`** · *engineer*
Expanded from `docs/ARCHITECTURE.md` §10: adding a route, a bounded context, an
API version, a connector, an LLM provider, a migration. Each as a numbered
recipe with the tests that will fail if a step is skipped — the failure is the
teaching mechanism.

**`engineering/testing-strategy.md`** · *engineer and technical leadership*
The four layers and the decision rule for where a test belongs. **Ports and
fakes** as the reason the fast suite needs no Docker, network, model or
credentials, and why a port with no fake is a design smell. The three coverage
guarantees, and the argument for them: line coverage tells you what *ran*, these
tell you what someone *decided to test*. What is deliberately untested and why.
Then **what to add as the platform grows**: contract tests against real connector
payloads, migration up/down/up testing on production-shaped data, property-based
tests for the filter policy, golden-file tests for prompt output shape, an
**evaluation harness for agent quality** (the filter's precision/recall against a
labelled set — currently nothing measures whether the policy works, only that
the plumbing does), load testing once ingestion is asynchronous, and security
testing of the auth matrix as scopes multiply.

**`engineering/database-learning-path.md`** · *engineer, explicitly for newcomers*
A guided study path, structured as **stages with a concrete exercise in this
repository at each stage** — not a link list. Cover, in this order:

1. **PostgreSQL fundamentals as used here** — types actually in the schema
   (`uuid`, `timestamptz`, `jsonb`, enums), constraints, indexes, `ON DELETE`
   behaviour. *Exercise: trace `0001_initial_schema.py` and predict what
   deleting a user does; then read `test_deleting_a_user_cascades_to_their_data`.*
2. **SQLAlchemy 2.0 declarative style** — `Mapped`/`mapped_column`, mixins,
   relationships, and `lazy="raise"`. *Exercise: remove a `selectinload` and
   watch the loud failure; explain why it is better than the alternative.*
3. **Async SQLAlchemy** — engine versus session versus connection, the pool,
   why the driver must be `asyncpg`, greenlet boundaries and what
   `MissingGreenlet` actually means. *Exercise: follow one request from
   `get_session` to a committed row.*
4. **Transactions** — autobegin, commit, rollback, savepoints and nesting, and
   why holding a transaction across a network call is a production incident
   waiting to happen. *Exercise: read `test_unit_of_work.py`, then find the
   comment in `ingestion/service.py` explaining the transaction shape.*
5. **Alembic** — revisions, autogenerate and its blind spots (it will not see
   an index it cannot reflect, or a data migration you need), offline mode,
   forward-only discipline. *Exercise: add a nullable column, autogenerate,
   read the diff critically.*
6. **Query patterns in this codebase** — `ON CONFLICT DO NOTHING` as an
   idempotency mechanism, the window function that avoids the N+1, aggregate
   counting, batched `OR`-of-`AND` lookups. *Exercise: write the equivalent
   N+1 version of `latest_for_users` and reason about the query count at
   page size 100.*
7. **pgvector** — what an embedding is, cosine versus L2 versus inner product,
   why normalised vectors make cosine an inner product, HNSW versus IVFFlat,
   `m` and `ef_construction`, and why an ANN index is approximate.
   *Exercise: read `test_the_hnsw_index_answers_a_nearest_neighbour_query` and
   design the search endpoint that does not yet exist.*
8. **Operating it** — `EXPLAIN ANALYZE`, connection-pool sizing, migrations
   under load, backups.

Point at primary sources (PostgreSQL manual, SQLAlchemy 2.0 ORM docs, Alembic
docs, pgvector README) rather than blog posts. End with a **"you are ready when
you can…"** checklist.

**`engineering/contributing.md`** · *engineer*
Branching, what `make check` runs, what a reviewer looks for, when a change
needs a migration, when it needs a new API version, when it needs a new row in
`AUTH_MATRIX`.

### Reference

**`reference/configuration.md`** — every environment variable: purpose, default,
valid values, which are secrets, which are unsafe in production and which
startup guard catches them. Source: `src/app/core/config.py` and `.env.example`.

**`reference/api-reference.md`** — the routes, narrated. Do not duplicate the
generated OpenAPI; link to it and explain what the generated document cannot:
which scopes, what the response *means*, what the counters in the ingestion run
report are for and which one to alert on.

**`reference/glossary.md`** — every term a newcomer will meet, technical and
domain, in one alphabetical list.

---

## 6. Style

- **Lead with the problem, then the mechanism.** "Awaiting a CPU-bound call
  stalls every other request; therefore an executor" — never the reverse.
- **Explain a decision by the alternative it rejected.** Every significant choice
  in this codebase has a comment saying what it avoids; carry that forward.
- **Diagrams where a diagram is genuinely clearer** — mermaid, since it renders
  on GitHub. Sequence diagrams for the two pipelines, an ERD for the schema, a
  layer diagram for the architecture. Not more than that.
- **Tables for anything enumerable** — scopes, routes, variables, milestones.
- **Code excerpts short and real.** Copy from the repository; never paraphrase
  code into something that would not run.
- **No marketing voice.** No "seamlessly", no "robust", no "cutting-edge". The
  design is interesting enough stated plainly.
- **Cross-link generously**, and link into the source tree by path.
- **Every page opens with who it is for and what they will know afterwards.**

---

## 7. Done when

- [ ] Every page in §5 exists, or its absence is justified in `docs/README.md`.
- [ ] Every technical claim is traceable to a file path, and spot-checking ten
      of them finds no errors.
- [ ] The eight open product questions appear as questions, unanswered.
- [ ] "Local embeddings, remote reasoning" is stated wherever privacy is
      discussed; no unqualified privacy claim survives.
- [ ] The stub provider's role is clear wherever "agentic" appears.
- [ ] No performance figure appears anywhere.
- [ ] A business reader can read `product/` alone and come away with an accurate
      picture, including of the limits.
- [ ] An engineer can read `engineering/getting-started.md` plus
      `architecture/overview.md` and make a correct first change.
- [ ] `platform/growth-path.md` gives a decision-maker a defensible sequence with
      reasoning, not a wishlist.
- [ ] `engineering/database-learning-path.md` has a concrete exercise in *this*
      repository at every stage.
- [ ] Nothing contradicts `docs/ARCHITECTURE.md` or `docs/PROTOTYPE-REPORT.md` —
      or, where it does, the contradiction is deliberate, flagged, and the
      superseded document is updated.

---

## 8. Existing documents, and what to do with them

| Document | Status | What to do |
| --- | --- | --- |
| `README.md` | Current and accurate | Keep as the repo front door; trim anything that moves into `docs/` and link out |
| `docs/ARCHITECTURE.md` | Current, engineer-facing | The primary source for `architecture/` and `engineering/conventions.md`. Either fold it in and leave a pointer, or keep it as the terse reference the long-form pages expand on — decide and say which |
| `docs/PROTOTYPE-REPORT.md` | Current audit + gap register | **Do not fold this away.** It is the gap register the growth path is built from. Keep it, and cite its finding ids (`R-1`, `S-1`, `P-3`) from the new pages so the two stay connected |
| `tests/README.md` | Current | The primary source for `engineering/testing-strategy.md`; keep it in place as the suite's own README |
