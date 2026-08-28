# Test suite

```
tests/
├── conftest.py            environment defaults, principals, app + client fixtures
├── factories/             entity builders (unpersisted, ready to assert on)
├── fakes/                 one double per port: LLM, embeddings, source, unit of work
├── unit/                  mirrors src/app/ - pure logic, no I/O
├── api/                   real routers + real auth, faked persistence
├── integration/           real PostgreSQL + pgvector via testcontainers
└── contract/              rules about the codebase itself
```

## The four layers, and what each is for

| Layer | Runs | Needs | Answers |
| --- | --- | --- | --- |
| `unit/` | milliseconds | nothing | Does this function do the right thing? |
| `api/` | milliseconds | nothing | Does the HTTP surface enforce scopes and publish the right shapes? |
| `contract/` | milliseconds | nothing | Does the codebase still obey its own rules? |
| `integration/` | ~20s + container start | Docker | Does the SQL actually work? |

`unit/` mirrors `src/app/` directory for directory, so the gap between a source
tree and a test tree is visible by looking at them side by side.

## Commands

```bash
make test              # unit + api + contract. No Docker, no network, no model.
make test-integration  # integration only (starts a pgvector container)
make test-all          # everything
make cov               # fast suite + coverage, HTML report in htmlcov/
```

## The three coverage guarantees

Line coverage tells you what *ran*. These tell you what someone *decided to
test*, which is the thing that goes stale silently.

1. **`contract/test_structure.py` → `SOURCE_COVERAGE`**
   Every module under `src/app` is mapped to the test modules that exercise it,
   or to `Untested("reason")`. Add a source file without a decision and the
   suite fails. Name a test file that does not exist and it fails.

2. **`contract/test_auth_matrix.py` → `AUTH_MATRIX`**
   Every route × every caller → expected status, asserted against the running
   app. `test_every_route_appears_in_the_matrix` fails when a route is added
   without declaring who may call it. This is the file to read when asking "who
   can do what".

3. **`contract/test_layering.py`**
   The dependency direction (`api → domains → core`), "no repository imports in
   routes", "no fastapi in domains", "no cross-version imports", and "every
   reading repository method routes through `scoped()`" - all AST-checked.

Line coverage has a floor of 85% (`pyproject.toml`, `[tool.coverage.report]`).
It is a floor, not a target: raise it when the suite genuinely improves, never
lower it to make a build pass.

## Why there is a fake for every port

`fakes/` holds one double per port the application defines - `LLMClient`,
`EmbeddingService`, `MessageSource`, and the unit of work. That is what lets the
whole fast suite run with no network, no model download, no credentials and no
database. If a new port appears and no double shows up here, that is the signal
it was not really designed as a port.

`FakeUnitOfWork` deserves the specific note: it makes the *pipeline* testable in
milliseconds - dedupe, fail-closed filtering, provisioning, dry run, idempotency
- while `integration/` separately proves the SQL underneath. Pipeline logic
changes weekly and must be fast to test; the window function and the upsert
change rarely and must be tested for real.

## What is deliberately untested, and why

Listed in `SOURCE_COVERAGE` as `Untested(...)`, currently:

- **`shared/embeddings/worker.py`** - loads the real sentence-transformer.
  Testing it would mean downloading a model in CI to assert that torch works.
  The *contract* around it (offload to the executor, ordering, dimension
  mismatch) is covered in `unit/shared/embeddings/test_service.py` with the
  torch call replaced.
- **`shared/llm/anthropic_client.py`** - a thin adapter over a paid network API.
  A test would exercise the vendor SDK, not us. The port contract is covered
  through the stub.
- **`core/db/base.py`, `core/db/mixins.py`, `shared/llm/base.py`** - declarations
  with no behaviour.

`test_untested_modules_stay_a_short_list` caps that list at six, so "untested"
cannot quietly become the default.

## Conventions

- Test names are sentences: `test_a_second_run_is_idempotent`, not `test_run_2`.
- A comment above a test says *why the behaviour matters*, not what the code does.
- Prefer a fake over a mock. If a fake is awkward to write, the seam is wrong.
- Integration tests run inside a transaction that is rolled back, so the schema
  is migrated once and no test can see another's rows.
