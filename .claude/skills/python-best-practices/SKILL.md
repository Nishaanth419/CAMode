---
name: python-best-practices
description: Enterprise Python coding standard for AI agents. Load BEFORE writing, modifying, or reviewing any Python code (.py files, FastAPI routes, SQLAlchemy models, pytest tests, Pydantic schemas, async code, Alembic migrations). Covers architecture layering, naming, type safety, error handling, logging, security, DB access, FastAPI, async, performance, testing, and tooling (ruff/black/mypy).
---

# Python Best Practices (Enterprise Standard)

Target: Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2.0, Alembic, pytest.
When this conflicts with an in-repo convention, follow the repo and say so.

## 1. Project Structure

```
app/
  main.py            # ASGI app factory, middleware, router registration only
  api/               # HTTP layer: routers, dependencies, exception handlers
    v1/routes/
    deps.py
  services/          # application services (use cases); orchestrate repositories
  repositories/      # all persistence access
  models/            # SQLAlchemy ORM entities (persistence shape)
  schemas/           # Pydantic request/response DTOs (wire shape)
  domain/            # pure business types, value objects, domain services, domain exceptions (no I/O)
  core/              # settings, logging config, security, constants
  db/                # engine/session factory, base metadata, migrations/ (Alembic)
  clients/           # outbound HTTP/queue/third-party adapters
  utils/             # small generic helpers
tests/
  unit/  integration/  e2e/  conftest.py  factories/
```

| Layer | May depend on | Must never |
|---|---|---|
| `api` | `schemas`, `services`, `deps` | touch `Session`, ORM models, or SQL |
| `services` | `repositories`, `domain`, `clients` | import FastAPI, `Request`, or `HTTPException` |
| `repositories` | `models`, `db` | contain business rules |
| `domain` | stdlib only | import SQLAlchemy, FastAPI, Pydantic-web concerns |
| `utils` | stdlib | import `app.*` |

- Services may receive ORM objects from repositories; there is no mapping layer between them.
- `models/` is not framework-independent: entity behaviour that must be unit-tested without a database belongs in `domain/`.
- Once a layer exceeds ~10 files, group it by aggregate/feature subpackage (`services/orders/`, `services/invoicing/`), not by pattern.

## 2. Naming and Imports

- Booleans take an `is_`/`has_`/`can_`/`should_` prefix.
- Units and currency belong in the name: `timeout_seconds`, `amount_inr_paise`.
- Don't encode the type in the name (`id_to_name_dict`, `order_list`).
- Absolute imports for first-party code; relative imports only inside a tightly cohesive package.
- Keep `__init__.py` thin: re-export only a curated public API, and define `__all__` when you do.
- Fix an import cycle by moving the shared type into `domain/` or depending on a Protocol; a function-local import is a last resort and carries a comment saying why.
- No import-time side effects: no DB or network connections, no `load_dotenv()` in library modules.

## 3. Functions

- ≤ 30 lines, ≤ 4 parameters, cyclomatic complexity ≤ 8; past that, extract.
- Type hints on every parameter and return value, including `-> None`.
- Optional and boolean parameters are keyword-only (`def send(*, dry_run: bool = False) -> None:`); never pass a bare boolean positionally.
- No `**kwargs` pass-through where real parameters belong.
- One return type: raise instead of returning a sentinel (not `Order | None | bool`); `X | None` means genuinely absent, never a way to dodge error handling.
- Never mutate a caller-owned argument unless the name says so (`_in_place`).

## 4. Classes

- A class with one method and no state is a function.
- **One class per file.** Each class lives in its own module, named after the class in snake_case (`OrderService` → `order_service.py`); five classes means five files. The only exception is a private helper (e.g. a small frozen dataclass) used exclusively by the class it sits next to.
- `@dataclass(frozen=True, slots=True)` for value objects; Pydantic `BaseModel` only at I/O boundaries (validation/serialization); plain classes for services.
- Inject collaborators (sessions, HTTP clients, settings) through `__init__`; never construct them inside the class.
- Inheritance depth ≤ 2.
- A class over ~200 lines or with > 7 public methods gets split.
- Define `__repr__`.

## 5. Type Safety

- PEP 695 syntax (`type Alias = ...`, `class Repository[T]:`, `def first[T](items: Sequence[T]) -> T | None:`) and builtin generics; never `List`, `Dict`, `Optional`, `Union`.
- `Protocol` (not ABC) for ports, defined in the inner layer; outer adapters satisfy it structurally without importing it.
- `TypedDict` for fixed-shape dicts crossing boundaries (external JSON). `NewType` for IDs (`CustomerId = NewType("CustomerId", int)`); a PEP 695 `type` alias cannot express it and does not stop IDs mixing.
- `Literal`/`Enum` instead of magic strings; `Final` on module constants.
- `Any` needs a justifying comment; prefer `object` plus narrowing, or one `cast()` at a well-marked seam. `mypy --strict` allows explicit `Any`, and ruff `ANN401` flags only a top-level `Any` (including `Any | None`) parameter or return annotation (a justified one carries `# noqa: ANN401`), so nested `Any` (`dict[str, Any]`), variable annotations, and `cast` are caught only in review unless mypy `disallow_any_explicit` is on.
- `assert isinstance(...)` narrowing only in tests; production code uses explicit checks that raise.

## 6. Error Handling

- One hierarchy rooted in `AppError`; `DomainError(AppError)` for business-rule violations; specific errors subclass per bounded context and carry the identifying values as attributes (`CreditLimitExceededError(requested, available)`).
- `except Exception` only at a top-level boundary (request handler, worker loop, CLI entrypoint), logging with `exc_info=True`, then re-raising or failing the unit of work.
- Exception messages never carry secrets, tokens, PII, or raw SQL.
- Retry only genuinely transient faults: bounded attempts, jittered backoff, idempotent operations only.

## 7. Logging

- One module-level `logger = logging.getLogger(__name__)`. `print()` is banned in application code (CLI user-facing output excepted).
- Event-name message plus structured extras: `logger.info("order_credit_blocked", extra={"order_id": order.id, "shortfall": str(gap)})`. Any interpolation is lazy `%s`, never an f-string.
- Correlation ID: middleware generates or propagates a request ID into a `ContextVar`, a `logging.Filter` injects it into every record, and outbound calls forward it as `X-Request-ID`.
- Mask at the logging layer (a redacting filter), not by trusting call sites. Never log passwords, tokens, API keys, full card/bank numbers, OTPs, auth headers, or full request bodies.
- Log security events: authentication success and failure, authorization failure, input-validation failure, and sensitive-data access, each with the actor, the source, and the correlation ID.
- No `INFO` inside loops.
- Configure handlers and format once at startup (`core/logging.py`); modules never call `basicConfig`.

## 8. Security

- Secrets load through a typed `pydantic-settings` `Settings` as `SecretStr`, kept out of `repr` and tracebacks. No hardcoded credentials, keys, or connection strings, not even in tests, defaults, or comments.
- Validate at the boundary with Pydantic (exact types, `Field` bounds, `extra="forbid"`, allow-lists over deny-lists) and trust the data inside the core.
- Uploads: allow-list extensions, verify the content matches the extension (magic bytes or a content-validation library), and enforce a size limit; never trust the client-supplied `Content-Type`.
- Password hashing, with the work factor always stated: **Argon2id** (m ≥ 19 MiB, t = 2, p = 1, or m ≥ 46 MiB, t = 1, p = 1); **scrypt** (N ≥ 2^17, r = 8, p = 1) if Argon2id is unavailable; **bcrypt** (cost ≥ 10, 72-byte input limit) for legacy systems only; **PBKDF2-HMAC-SHA-256 ≥ 600,000 iterations** (SHA-512 ≥ 210,000) where FIPS-140 compliance is required.
- Credential policy: minimum 8 characters (15 strongly recommended where MFA is absent), permit at least 64, no composition rules, failed-login counter bound to the account not the source IP, generic login errors.
- JWTs: verify signature, `alg`, `aud`, `iss`, and `exp`; short TTL; no secrets in the payload. Compare secrets with `hmac.compare_digest`.
- AuthZ: deny by default, enforced on every endpoint except the unauthenticated health probes by a dependency (router-level `dependencies=[Depends(require_role(...))]` for cross-cutting auth); object-level ownership/tenancy checked in the service or repository query (`WHERE tenant_id = :tenant`).
- Security headers: HSTS, a CSP with `frame-ancestors`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`. CORS: explicit origin allow-list, no wildcard origin on any response carrying sensitive data, and never `allow_origins=["*"]` with credentials.
- TLS certificate verification always on: never `verify=False`.
- SSRF and path traversal: allow-list outbound URLs; confine file paths with `Path.resolve().is_relative_to(base)`.

## 9. Database (SQLAlchemy 2.0 + Alembic)

- Repositories expose intent-named methods (`find_unpaid_by_customer(customer_id)`) and are the only code that builds or executes queries. Services touch the session only to own the transaction and hand it to repositories; routes never receive it.
- 2.0 style: `DeclarativeBase` with `Mapped[...]`/`mapped_column(...)`; `select()` executed via `session.scalars(stmt)`; never the legacy `Query` API.
- One transaction per unit of work (`with session.begin():` or a `unit_of_work` context manager); repositories never commit; never `commit()` per record inside a loop (a batch job commits once per batch). A unit of work spanning several aggregates usually means wrong boundaries; make cross-aggregate consistency asynchronous.
- One session per request from a `yield` dependency (`with Session(engine) as session: yield session`). Never a module-global session, and never one shared across threads or concurrent tasks (an `AsyncSession` inside a `TaskGroup` included).
- Pool: set `pool_size`, `max_overflow`, `pool_pre_ping=True`, and `pool_recycle` to stated values, overriding the defaults (5, 10, -1, pre-ping off). `pool_size`/`max_overflow` apply only to `QueuePool` (`AsyncAdaptedQueuePool` under `create_async_engine`), not to `NullPool` or the `SingletonThreadPool` used for SQLite `:memory:` tests.
- Relationships: `lazy="raise"` by default, loading explicitly with `selectinload`/`joinedload`; `lazy="raise_on_sql"` is a different strategy, not a synonym. Raise-loading does not apply within the unit-of-work flush, so a lazy load that `Session.flush()` needs still runs. Always define `back_populates` and an explicit `cascade`/`passive_deletes` policy.
- Read-modify-write on money or stock: `with_for_update()` or optimistic versioning (`version_id_col`).
- Python side: `Decimal` or `int` minor units for money, never `float`; timezone-aware UTC `datetime`s.
- Every schema change ships an Alembic revision: autogenerate, then hand-edit; a working, tested `downgrade()`.

## 10. FastAPI

- Thin routes: validate input via schema, call one service method, return a response model. ≤ 15 lines, zero business logic, `if` chains, or SQL.
- `Annotated[X, Depends(...)]` for current user, settings, and services.
- Annotate the return type (`-> OrderOut`); add `response_model=` only where the response genuinely differs from what the function returns (it wins if both are set). Always set `status_code` explicitly. Omit absent members rather than emitting `null`: `response_model_exclude_none=True` (`exclude_unset` where a `null` is a documented state).
- Separate `...In`/`...Out`/`...Patch` schemas; never return ORM objects from a route.
- `@app.exception_handler`s for `DomainError`, validation errors, and a catch-all emit `application/problem+json` (RFC 9457). They are the only place exceptions become status codes. The `RequestValidationError` handler returns `422` for body errors and `400` for query, path, and header errors (`loc[0]`). FastAPI ignores unknown query parameters: reject them with a query model set to `extra="forbid"`, and clamp an oversize `page_size` in code rather than rejecting it with `Query(le=...)`.
- `async def` for awaitable I/O and for handlers that do no I/O at all; plain `def` (FastAPI runs it in a threadpool) when the work blocks. Unsure: plain `def`.
- `BackgroundTasks` only for short, fire-and-forget, failure-tolerant work (emails, cache warm). Anything retryable, long, or business-critical goes to a real queue/worker.
- Startup/shutdown via the lifespan context manager.
- Middleware for request ID, timing, the error boundary, and `Idempotency-Key` replay (only middleware sees the final status, headers, and body to store), never business rules. The catch-all `Exception` handler runs outside all middleware, so replay middleware records an unhandled error's `500` problem itself.

## 11. Async

- Nothing blocking in `async def` (`time.sleep`, `requests`, sync DB drivers, heavy file I/O): use `asyncio.sleep`, `httpx.AsyncClient`, async drivers (`asyncpg`), or `await asyncio.to_thread(...)`; CPU-bound work goes to a `ProcessPoolExecutor` or a worker.
- One stack per project: sync SQLAlchemy with sync routes, or `AsyncSession` with async routes.
- Concurrency via `asyncio.TaskGroup`; `gather(..., return_exceptions=True)` only when deliberately handling partial failure.
- Never a bare `asyncio.create_task`: keep a reference, await it, or use a TaskGroup, or it can be garbage-collected and its exception vanishes.
- Every outbound call gets a timeout (`asyncio.timeout()` or the client's). On `asyncio.CancelledError`, clean up and re-raise; never swallow it.
- Reuse one `AsyncClient`/pool for the app lifetime.
- Request-scoped context lives in `ContextVar`s, never globals.

## 12. Performance

- Partial selects on wide tables (`select(Order.id, Order.total)`) only on read paths feeding a response schema; never mutate through one: writes go through a fully loaded aggregate.
- Bulk operations for volume: `insert().values([...])`, `session.execute(insert(Model), rows)`, `session.execute(update(...))`, `COPY` for very large loads.
- Stream large results (`yield_per()`/`session.stream()`, `StreamingResponse` for exports); never `.all()` an unbounded table.
- Every cache has an explicit key, TTL, and invalidation story: `functools.cache`/`lru_cache` for pure in-process functions, Redis for shared/cross-process. Never cache per-user data under a global key.
- Optimize only with a measurement (`cProfile`, `EXPLAIN ANALYZE`, timing logs), and state the numbers in the PR.

## 13. Testing

- pytest only. `tests/` mirrors `app/`; files `test_<module>.py`; tests `test_<unit>_<scenario>_<expected>()` (`test_reserve_inventory_when_out_of_stock_raises`).
- Unit tests for services/domain: no DB, no network; fake repositories through their Protocols.
- Integration tests for repositories and routes: a real database (testcontainers or a disposable schema), transaction-rollback fixtures for isolation, `TestClient`/`httpx.ASGITransport` for the app. Migration tests run on their own empty database: `CONCURRENTLY` cannot run inside a fixture's transaction.
- Mock at the boundary you own (repository, client interface), not deep internals; prefer fakes over `MagicMock` for anything with behaviour; `respx`/`responses` for HTTP; never hit real third parties.
- Cover empty/one/many, boundary values, `None`/missing, duplicates, unicode, `Decimal` rounding, timezone/DST, concurrent update, permission denied, upstream timeout, and every raised exception path.
- Tests are deterministic (injected clock or `freezegun`, seeded randomness, no `sleep`, no ordering dependence, no shared mutable module state), and every bug fix ships a regression test that fails before the fix.
- `pytest.mark.parametrize` over copy-pasted cases; factories/builders (`factory_boy` or plain helpers) over giant literal fixtures.
- Register every custom marker (`slow`, `integration`) in `[tool.pytest.ini_options] markers = [...]` with strict marker validation on.
- Coverage ≥ 85% overall, ~100% on services/domain.

## 14. Code Quality Gates

All pass locally and in CI before code is complete:

```bash
ruff format .                 # local (or: black .); CI: ruff format --check .
ruff check --fix .            # local only
ruff check .                  # CI: no --fix; it rewrites the checkout and exits 0
mypy --strict app
pytest --cov=app --cov-fail-under=85 -q   # --cov* come from pytest-cov; declare that dependency
pip-audit
alembic upgrade head
```

- In a repo not yet clean under these tools, fix lint and type findings only in lines you touch and format only those lines (`ruff format --range`, one file per call); a whole-file cleanup is its own commit and PR.
- Ruff rules: `E,F,I,N,UP,B,S,SIM,C4,RET,ARG,PTH,ASYNC,ANN`, optionally plus `C90` with `[tool.ruff.lint.mccabe] max-complexity = 8`. Narrow `E` to `E4,E7,E9` or add `E111,E114,E117,W191,E501` to `lint.ignore`; those codes conflict with the formatter. `S` flags every pytest `assert` (S101): add `[tool.ruff.lint.per-file-ignores] "tests/**" = ["S101"]`.
- Config lives in `pyproject.toml`: `[tool.mypy] strict = true`; line length 100 set explicitly under `[tool.ruff]`/`[tool.black]`, or the tools silently enforce 88. Formatters do not reflow comments or docstrings; keep those narrower than code by hand.
- Pin the ruff, black, and mypy versions in `pyproject.toml` (the flag set behind `--strict` changes between mypy releases) and lock dependencies with a lockfile.
- Zero warnings (`filterwarnings = ["error"]` under `[tool.pytest.ini_options]`), zero skipped tests. Every new `# noqa`/`# type: ignore` names its code (`# type: ignore[arg-type]`) and carries a reason comment. A failing gate is not "unrelated" until proven so.

## 15. Documentation

- Docstrings on every public module, class, function, and method, including `__init__`: a one-line summary in the imperative mood ("Return", not "Returns"), then `Args:`/`Returns:` (or `Yields:`)/`Raises:`. Sections may be omitted where the name and signature are informative enough; a trivial function may skip the docstring.
- `Raises:` lists only the exceptions in the function's contract: the `ValueError` from an argument precondition stays out.
- Docstrings state the contract and why, never the signature or the types.
- Inline comments only for non-obvious rationale (a business rule, a workaround with a link, a deliberate perf trade-off). Delete commented-out code; every TODO names an owner or ticket.
- Adding a setup step, env var, command, or service updates the README/`docs/`; new env vars go in `.env.example` in the same change.
- FastAPI routes carry `summary`, `description`, `responses={...}` (4xx/5xx declared as `application/problem+json` under `content`, since `model=` files them under `application/json`; FastAPI keeps its default `422` schema unless `422` or `4XX` is declared), and schema `Field(examples=...)`; export `app.openapi()` to the committed spec file the contract gates diff.
- Record non-obvious architectural decisions as a short ADR in `docs/adr/`.

## 16. Agent Rules

- Never invent business rules: if credit limits, tax treatment, rounding, statuses, or SLAs are ambiguous, ask; do not silently pick a default.
- Never break a public Python API (signature, default, return type, import path) to apply a rule here (one class per file, keyword-only booleans, raise-not-sentinel): keep a re-export or deprecation shim and say so.
- Add no dependency without saying why and confirming; prefer the stdlib.
- Flag anything security-relevant you touch (auth, tenancy, input handling, SQL, file paths, outbound URLs) in the summary.
