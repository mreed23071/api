# Architecture & conventions

The prototype exists to test an idea. This document exists so the *foundation*
under it is the one we would build the real product on: a properly versioned
API, a predictable file layout, an auth model with a real principal, and a test
suite whose coverage can be read at a glance.

Read this before adding anything. The rules below are not style preferences -
most of them are enforced by `tests/contract/`, so breaking one fails the build.

---

## 1. Layering

```
        ┌──────────────────────────────────────────────┐
        │  api/          HTTP. Versioned. Knows FastAPI │
        │                and nothing about SQL.         │
        └───────────────────┬──────────────────────────┘
                            │  services, DTOs
        ┌───────────────────▼──────────────────────────┐
        │  domains/      Business logic, one package    │
        │                per bounded context.           │
        └───────────────────┬──────────────────────────┘
                            │  Base, Repository, errors, Principal
        ┌───────────────────▼──────────────────────────┐
        │  core/         Infrastructure. Knows nothing  │
        │  shared/       about any specific domain.     │
        └──────────────────────────────────────────────┘
```

Dependencies point **downwards only**:

| Rule | Enforced by |
| --- | --- |
| `core/` never imports `domains/` or `api/` | `test_core_never_imports_a_bounded_context_or_the_api` |
| `domains/` never imports `api/` | `test_domains_never_import_the_api_layer` |
| `domains/` never imports `fastapi` | `test_domains_never_import_fastapi` |
| `shared/` never imports `domains/` or `api/` | `test_shared_never_imports_a_bounded_context_or_the_api` |
| Routes call services, never repositories | `test_routes_go_through_services_never_repositories` |
| A version never imports another version | `test_a_version_never_imports_another_version` |
| Every reading repository method uses `scoped()` | `test_every_reading_repository_method_routes_through_scoped` |

The practical payoff: a service can be called from a queue worker, a CLI or a
test with no HTTP in scope - which is exactly what will be needed when ingestion
becomes a background job.

---

## 2. Directory map

```
src/app/
├── main.py                  app factory, lifespan, startup guards
├── models.py                import surface for Alembic autogenerate
│
├── api/                     ── the HTTP layer ──
│   ├── deps.py              composition root: builds services from request scope
│   ├── errors.py            the single error envelope + exception handlers
│   ├── system.py            unversioned /health and /ready
│   ├── router.py            version registry, mounting, deprecation headers
│   └── v1/
│       ├── routes/          one module per bounded context
│       └── schemas/         the wire contract THIS version publishes
│
├── core/                    ── infrastructure ──
│   ├── config.py            typed settings + production guards
│   ├── errors.py            the exception hierarchy
│   ├── logging.py           JSON logs, request/run correlation
│   ├── middleware.py        request id + access log
│   ├── openapi.py           operation-id policy, per-version export
│   ├── pagination.py        PageParams / Paginated
│   ├── db/                  base, mixins, engine, repository, unit of work
│   └── security/            Principal, Scope, TenantContext, auth providers
│
├── domains/                 ── bounded contexts ──
│   ├── uow.py               the unit of work that aggregates repositories
│   ├── identity/            User, UserRelation - who a person is
│   ├── messaging/           Message + embeddings - what they said
│   ├── ingestion/           Domain 1: cron → filter → embed → store
│   └── insights/            Domain 2: retrieve → summarize
│
└── shared/                  ── ports with swappable adapters ──
    ├── llm/                 LLMClient port; stub and Anthropic adapters
    └── embeddings/          executor-backed local encoder
```

### A bounded context always looks the same

```
domains/<context>/
├── __init__.py     states what the context owns and publishes (asserted non-empty)
├── models.py       SQLAlchemy entities            (only if it owns tables)
├── dto.py          internal contracts it publishes to other contexts
├── repository.py   the only place SQL is written  (only if it owns tables)
└── service.py      business logic; asserts scopes; owns transactions
```

`identity` and `messaging` are pure domain contexts with no HTTP surface.
`ingestion` and `insights` are application contexts that compose them.

---

## 3. API versioning

**Paths.** Versions mount under `API_ROOT_PREFIX` (default `/api`), so v1 lives
at `/api/v1`. `GET /api/versions` advertises what the service speaks, each
version's status (`stable` / `preview` / `deprecated`) and its sunset date.

**The registry.** `app/api/router.py` holds `API_VERSIONS` - one tuple, the
single source of truth. Every response of a version carries `X-API-Version`, and
a deprecated version additionally carries `Deprecation` and `Sunset` headers.

**Wire schemas belong to the version.** `api/v1/schemas/` is frozen once v1
ships: additive changes only. Domain DTOs under `domains/*/dto.py` carry no such
promise. That asymmetry is the whole point of keeping them apart - refactor a
service freely, and the v1 contract still holds because the mapping layer
absorbs it.

### Adding v2

1. `cp -r api/v1 api/v2`.
2. Delete the schemas and routes v2 does not change; re-export them from v1.
3. Register the version in `API_VERSIONS`.
4. Rename any endpoint function whose shape changed (see operation ids below).
5. Mark v1 `DEPRECATED` with a `sunset` date when it is time.

v1 keeps working until its sunset date passes. `test_a_version_never_imports_
another_version` keeps them independently deletable.

### Operation ids, and the trade-off we took

FastAPI's default operation id is `"{name}_{path}_{method}"`, which generates
`listUserSummariesApiV1InsightsUsersGet()`. We override it to the bare function
name, so the SDK reads `listUserSummaries()`.

The cost: the function name is now the only thing keeping ids unique **across
versions**. Two options existed:

1. **Globally unique function names** (chosen). A v2 endpoint whose shape
   changes is named `list_user_summaries_v2`; one that does not change is
   re-exported from v1. `assert_unique_operation_ids()` runs in `create_app()`,
   so a collision is a startup failure, not a route silently missing from the
   generated client.
2. Prefix ids with the version (`v1ListUserSummaries()`). Unambiguous, uglier at
   every call site.

Because clients pin a version, `scripts/export_openapi.py` writes one document
per version (`openapi/v1.json`) and each SDK contains only its own operations.

### What counts as breaking

Breaking (needs a new version): removing or renaming a field, narrowing a type,
adding a required request field, changing a status code, tightening auth on an
existing route.

Not breaking: adding an optional request field, adding a response field, adding
a route, adding an enum member that only the server produces *if* clients were
told to tolerate unknown values.

---

## 4. Authentication and authorization

### The Principal

`core/security/principal.py` defines the object every other decision hangs off:

```python
Principal(subject, kind, scopes, tenant, auth_scheme)
```

It is resolved once at the edge and passed **into** the service layer.
Authorization is therefore a domain concern in domain vocabulary, not a boolean
at the door that nothing downstream can see:

```python
async def list_users(self, params: PageParams) -> Paginated[User]:
    self.principal.require(Scope.INSIGHTS_READ)
    ...
```

Routes *also* declare their scopes (`dependencies=[Depends(require_scopes(...))]`)
so the requirement appears in the OpenAPI document and fails before any service
is constructed. Both layers assert: the route for the contract, the service for
the guarantee.

`require()` distinguishes 401 from 403 deliberately. Anonymous → 401 ("you have
not identified yourself"); authenticated but under-scoped → 403. Conflating them
leaks whether a resource exists.

### Scopes name actions, not roles

`ingest:run`, `ingest:read`, `insights:read`, `messages:read`, `admin`.

Roles are a mapping from a name to a set of scopes and belong in whatever
identity provider issues credentials. Keeping them out of the codebase is what
lets the IdP change without a code change.

### Providers

`AuthProvider` is a port. Two adapters ship:

- **`ApiKeyAuthProvider`** - scoped static secrets for machine callers, from
  `API_KEYS`. Constant-time comparison against *every* record, so timing does
  not reveal which key matched. Accepts the legacy `X-Cron-Token` header so the
  existing scheduler keeps working.
- **`DevUserAuthProvider`** - `X-Dev-User` header impersonation for local work
  and API tests. The startup guard refuses to boot with it enabled in
  production.

A provider that sees its scheme but a bad credential **raises** rather than
declining, so one request cannot probe every scheme in the chain.

### The path to real authentication

Write `OidcAuthProvider` implementing `authenticate(request) -> Principal | None`
- validate the bearer JWT against a cached JWKS, map claims to scopes, map the
tenant claim to `TenantContext` - and list it in `build_auth_chain()`. Nothing
else changes: no route, no service, no test. That is the whole reason the seam
exists now rather than later.

Known limits of what ships today, tracked as A-2 in the prototype report: static
keys have no expiry, no rotation and no revocation list.

---

## 5. Tenancy

threadline is single-tenant today. `TenantContext` and `Repository.scoped()` exist
anyway, because retrofitting a tenant argument through every repository later is
the expensive version of this change and implementing one method body is the
cheap one.

To switch multi-tenancy on:

1. add `organization_id` to the tables (one migration),
2. give the models a `TenantScopedMixin`,
3. implement the body of `Repository.scoped()`,
4. every existing query becomes tenant-safe.

Step 4 is only true because every reading method routes through `scoped()`,
which `test_every_reading_repository_method_routes_through_scoped` enforces. A
non-global `TenantContext` reaching the repository layer today raises
`NotImplementedError` rather than silently returning everyone's data.

---

## 6. Transactions

`get_session` guarantees rollback and close; it never commits. **Services own
transaction boundaries**, through `UnitOfWork.transaction()`:

```python
async with self.uow.transaction():
    ...                      # committed on clean exit, rolled back on exception
```

Nested calls open a SAVEPOINT, so a service can compose operations that each
declare their own boundary.

Why this is explicit rather than automatic: the ingestion pipeline does its
source fetch, its LLM round trips and its embedding batch **outside** any write
transaction, and opens one only for provisioning and the final insert. A
transaction held open across a network call pins a pooled connection and holds
row locks for as long as the provider takes to answer.

---

## 7. Errors

One hierarchy (`core/errors.py`), one envelope, one place that maps failures to
HTTP (`api/errors.py`):

```json
{ "error": { "code": "forbidden", "message": "...", "details": {...},
             "request_id": "..." } }
```

`ErrorCode` values are part of the public contract - clients branch on them.
Add members freely; never rename one. Every route documents 401 and 403 in its
OpenAPI responses, so the generated client has exactly one error type to narrow
against (`test_every_operation_documents_the_error_envelope`).

Unhandled exceptions return a deliberately opaque 500: an unexpected exception's
message can carry connection strings, row contents or provider payloads. The
`request_id` is how a user reports it and how you find it in the logs.

---

## 8. Configuration and startup guards

Every setting is an environment variable read once into a cached `Settings`.
`validate_for_environment()` runs in `create_app()` and **refuses to start** on:

- a wildcard CORS origin combined with credentials (any environment);
- an API key that grants no scopes (any environment);
- the repository's default `CRON_TOKEN` in production;
- `DEV_AUTH_ENABLED` in production;
- public docs in production;
- `LLM_PROVIDER=anthropic` with no key in production.

Each of these exists because the failure it prevents is silent: a service that
boots happily and is wide open.

---

## 9. Observability

Logs are JSON with the ambient `request_id` (and `run_id`, during ingestion)
folded in by the formatter, so no call site threads them through. An inbound
`X-Request-Id` is honoured, so a trace started at the ingress survives.

Still missing, and the next thing to add: metrics. Ingestion duration, filter
keep-rate, LLM error rate, embedding queue depth. The keep-rate in particular is
the signal that tells you a prompt change broke retention.

---

## 10. Recipes

### Add a route to an existing context

1. Add the wire schema to `api/v1/schemas/<context>.py`.
2. Add the handler to `api/v1/routes/<context>.py`, with
   `dependencies=[Depends(require_scopes(...))]`.
3. Assert the same scope inside the service method.
4. Add a row per caller to `AUTH_MATRIX` in `tests/contract/test_auth_matrix.py`
   (the suite fails until you do).
5. Add the module to `SOURCE_COVERAGE` if it is new.
6. `make openapi` and commit the schema.

### Add a bounded context

1. `domains/<name>/` with `__init__.py` (non-empty - say what it owns),
   `models.py`, `dto.py`, `repository.py`, `service.py`.
2. Import its models in `app/models.py` so Alembic can see the tables.
3. Register its repositories on `UnitOfWork`.
4. `make revision m="add <name>"`, review the generated migration by hand.
5. Add `api/v1/routes/<name>.py` and `api/v1/schemas/<name>.py`, include the
   router in `api/v1/__init__.py`.

### Add a connector

Implement `MessageSource` (`domains/ingestion/sources.py`) and return it from
`get_message_source()` in `api/deps.py`. A real connector will also need an
incremental cursor - that belongs on the protocol as a `fetch(since=...)`
argument plus a table to persist the watermark.

### Add an LLM provider

Implement `LLMClient` in `shared/llm/`, list it in `build_llm_client()`. Both
agents inherit it.

---

## 11. What this foundation still does not do

The gaps are catalogued in [`PROTOTYPE-REPORT.md`](PROTOTYPE-REPORT.md). The
ones that shape the next milestone:

- **Ingestion is synchronous** (R-1). Fine for a fixture connector; the first
  real one needs a queued job and an `ingestion_runs` table.
- **Vectors are written and never read** (S-1). The HNSW index is maintained at
  real cost with no consumer. Either ship the search endpoint or drop the column.
- **Summaries are recomputed on every request** (S-2). They should be persisted
  against the inputs that produced them.
- **Nothing has been executed.** No dependency has been installed, no container
  built, no test run - package registries were unreachable from the environment
  this was written in. Everything here is verified by static analysis only.
