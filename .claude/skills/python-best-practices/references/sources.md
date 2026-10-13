# Python Best Practices (Enterprise Standard) — Sources

Provenance for [`../SKILL.md`](../SKILL.md). This file is deliberately kept
out of the skill body so it costs no tokens at load time; read it to verify a
rule, not to follow one. Repository-wide provenance tiers and known gaps:
[SOURCES.md](../../../SOURCES.md).

Throughout this file, "this document" and section references like §4 point to
`../SKILL.md`, whose rules these sources support. This text was moved out of that
file verbatim, so "the rules above" likewise means the rules there.


This document was written on **2026-07-31** as a synthesis of established standards, from practice and
memory: no URLs were captured, and no page was open while it was written. The references below were
retrieved and read on **2026-08-20** to put that provenance on a verifiable footing. Every page listed
here was actually opened on that date; nothing is listed on the strength of a search result. That pass
also exposed places where this document diverges from a source it names — those are recorded in the
entries below, not smoothed over. SKILL.md itself carries no provenance since the 2026-09-28 trim; a
reference marked "(trimmed from SKILL.md 2026-09-28)" points to a rule that is no longer in it.

The 2026-09-28 trim also introduced a few facts that were not in the 2026-07-31 text: RFC 9457
`application/problem+json`, the 2.0 ORM bulk insert form, the scope of ruff `ANN401` and mypy
`disallow_any_explicit`, the `ruff check --fix` exit code, the mypy `arg-type` code, the second
Argon2id setting given as an equal alternative, and the claim that a `type` alias does not stop IDs
mixing. Those were checked on **2026-09-28**. Entries or sentences marked "Checked 2026-09-28" or
"Re-read 2026-09-28" were opened and read on that date. Every other verification in this file
dates from 2026-08-20.

A cross-skill review the same day added more facts: the FastAPI and Starlette behaviour behind
§10's idempotency middleware, `loc[0]` status split, unknown query parameters,
`exclude_none`/`exclude_unset` and default `422` schema; §13's `CONCURRENTLY` reason; and §14's
`ruff format --check`, `ruff format --range`, `S101` per-file ignore and `filterwarnings` settings.
Those were also checked on **2026-09-28**, partly by running the tools in a scratch directory
(ruff 0.16.9; fastapi 0.141.1 with starlette 1.7.0 and pydantic 2.13.5; pytest 9.1.1; PostgreSQL
14.20). Each entry labels tool output as such.

**Language and style**

- [PEP 8 — Style Guide for Python Code](https://peps.python.org/pep-0008/) — the casing conventions
  (trimmed from SKILL.md 2026-09-28) ("function names should be lowercase, with words separated by
  underscores", "class names should normally use the CapWords convention", constants "written in all
  capital letters with underscores"), the `l`/`O`/`I` single-character prohibition, and the
  trailing-underscore remedy for keyword clashes ("`class_` is better than `clss`") (both trimmed from
  SKILL.md 2026-09-28). Also the source §14 diverges from: 79 characters for code, 72 for
  comments and docstrings, 99 only by team agreement. It supplies nothing in §15 beyond the existence
  of docstrings, and does **not** define `Args`/`Returns`/`Raises`. A living "Active" PEP with no
  version number; page footer read "Last modified: 2025-04-04". Checked 2026-08-20.
- [PEP 257 — Docstring Conventions](https://peps.python.org/pep-0257/) — §15's three real PEP 257
  rules: docstrings on modules, on everything a module exports, and on public methods including
  `__init__`; the imperative-mood requirement; the one-line-summary shape; and "the one-line docstring
  should NOT be a 'signature' reiterating the function/method parameters". Its documentation
  requirement is content-level only — summarize behaviour and document arguments, return values, side
  effects and exceptions — with **no** named-section syntax. Footer read "Last modified: 2024-04-17".
  Checked 2026-08-20.
- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) — the actual source of
  §15's docstring *format* (3.8.3: `Args:`, `Returns:`/`Yields:`, `Raises:`, the mandatory-docstring
  trigger, the "not every exception" rule), §2's don't-encode-the-type rule from "names to avoid"
  (3.16.2), the naming table (3.16.1, 3.19.6 for CapWords type aliases) (trimmed from SKILL.md
  2026-09-28), a mutable-default ban (2.12.4) that SKILL.md no longer carries (already absent before
  the 2026-09-28 trim), and §15's TODO-needs-a-ticket rule (3.12). Also the 80-character limit §14
  diverges from, and the "about 40 lines" soft guidance §3 tightens to 30. The page publishes no version or revision date; it was dated indirectly from the
  upstream `google/styleguide` commit for `pyguide.md` (latest `ff7ea9c951eb`, 2025-02-24), so treat
  "current" with that caveat. Note it recommends pylint as the linter, which this document does not
  use. Checked 2026-08-20.

**HTTP**

- [RFC 9457 — Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc9457.html) — §10's
  error-body media type. The header reads "Obsoletes: 7807", "Category: Standards Track", July 2023,
  and the abstract says "This document obsoletes RFC 7807". §3 says the JSON object, "when
  serialized in a JSON document", is "identified with the 'application/problem+json' media type", and
  §6 records that IANA updated that registration to point at this document. That is why §10 now
  names RFC 9457 rather than the "RFC-7807 style JSON" of the 2026-07-31 text. The RFC defines the
  format. It says
  nothing about FastAPI, so the rule that only `@app.exception_handler`s produce it is a house layering
  rule. The api-contract-design-best-practices skill owns the problem-body contract and cites the same
  RFC. Checked 2026-09-28.

**Formatting, linting, typing**

- [Black — The Black code style: Current style](https://black.readthedocs.io/en/stable/the_black_code_style/current_style.html)
  — Black's default line length of 88 and its rationale, its explicit caution about lines beyond 100
  characters, and "style configuration options are deliberately limited and rarely added", which is
  what backed "the formatter is the sole authority" (trimmed from SKILL.md 2026-09-28). Docs "stable" channel, self-reported Black
  26.5.1. Checked 2026-08-20.
- [Ruff — Rules](https://docs.astral.sh/ruff/rules/) — confirms all fourteen rule families §14 selects
  exist and are what the comment claims (`E` pycodestyle, `F` Pyflakes, `I` isort, `N` pep8-naming,
  `UP` pyupgrade, `B` bugbear, `S` bandit, `SIM`, `C4` comprehensions, `RET`, `ARG`, `PTH`, `ASYNC`,
  `ANN`). No invented codes. Checked 2026-08-20.
- [Ruff — Settings](https://docs.astral.sh/ruff/settings/) — `line-length` defaults to 88, governs
  `E501` and where the formatter and isort wrap, and "isn't a hard upper bound, and formatted lines may
  exceed the line-length". The first is load-bearing in §14; the second backed an `E501` caveat
  (trimmed from SKILL.md 2026-09-28). Checked 2026-08-20. Re-read 2026-09-28 for §14's `S101`
  per-file ignore. `per-file-ignores` is "A list of mappings from file pattern to rule codes or
  prefixes to exclude, when considering any matching files", in globset syntax, and the example's
  table header is `[tool.ruff.lint.per-file-ignores]`. A ruff 0.16.9 run the same day with
  `"tests/**" = ["S101"]` silenced `S101` in `tests/test_b.py` and `tests/unit/test_a.py`, run
  from the project root and from inside `tests/`, and still reported it in `app/`.
- [Ruff — Default rules](https://docs.astral.sh/ruff/default-rules/) — what is on without
  configuration, and the reason §14's explicit `select` list is not redundant. The page publishes the
  default as an enumeration of individual codes (413 of them on this reading), not of families, so
  "family X is on by default" is nearly always false: isort contributes only `I001`, pep8-naming only
  `N999`, flake8-bandit only `S102`/`S110`/`S112`, flake8-return only `RET501`, flake8-use-pathlib only
  `PTH124`/`PTH210`, and `ANN` nothing at all. pycodestyle contributes only `E722` and `E902` — no `E4`
  rule, and none of the formatter-conflicting `E111`, `E114`, `E117`, `W191`, `E501`.
  Checked 2026-08-20.
- [Ruff — Tutorial](https://docs.astral.sh/ruff/tutorial/) — the page that actually carries the
  sentence SKILL.md used to quote (trimmed from SKILL.md 2026-09-28) about why the default set looks
  the way it does: Ruff enables its defaults
  "omitting any stylistic rules that overlap with the use of a formatter, like `ruff format` or Black".
  Recorded separately because the two pages are in tension: the tutorial summarises the default as the
  "`F`, `E`, `B`, `UP`, and `RUF` categories, as well as many more", which reads as whole families,
  while the Default rules page enumerates individual codes and takes almost nothing from `E`. Where
  they disagree, this document follows the enumeration. Checked 2026-08-20.
- [Ruff — Formatter](https://docs.astral.sh/ruff/formatter/) — `ruff format` as "a drop-in replacement
  for Black" with "near-identical output", which licenses §14's `ruff format .` / `black .` equivalence;
  and the list of lint rules to avoid alongside a formatter (`E111`, `E114`, `E117`, `W191`, plus the
  `E501` conflict), which is what §14's narrowed `E` selection responds to. Checked 2026-08-20.
  Re-read 2026-09-28 for §14's CI comment: `ruff format --check` "will avoid writing any formatted
  files back, and instead exit with a non-zero status code upon detecting any unformatted files".
  It exits `0` when no file would be formatted, `1` when one or more would be, and `2` on abnormal
  termination. A ruff 0.16.9 run reported "2 files would be reformatted", exited 1, and left both
  files unchanged. The page does not mention `--range`; the Configuring Ruff entry below covers it.
- [Ruff — Configuring Ruff](https://docs.astral.sh/ruff/configuration/) — §14's
  `ruff format --range`. The page's `ruff format --help` listing has `--range <RANGE>` under
  "Editor options": "When specified, Ruff will try to only format the code in the given range. It
  might be necessary to extend the start backwards or the end forwards, to fully enclose a logical
  line." That listing is abridged. The one-file-per-call limit comes from ruff 0.16.9's own
  `ruff format --help` ("The option can only be used when formatting a single file") and a run on
  2026-09-28. `--range` with two files, or with a directory, failed with "The `--range` option is
  only supported when formatting a single file but the specified paths resolve to 2 files" and
  exit 2. With one file it reformatted only the lines in the range. Checked 2026-09-28.
- [Ruff — `assert` (S101)](https://docs.astral.sh/ruff/rules/assert/) — §14's `S101` note. The rule is
  derived from flake8-bandit. "What it does" reads "Checks for uses of the `assert` keyword", because
  assertions "are removed when Python is run with optimization requested". Its only exemption is
  "assertions within a `TYPE_CHECKING` block", so every pytest `assert` is flagged. A ruff 0.16.9
  run (`--isolated --select S101`) flagged the `assert` in both test files and in `app/`. Checked
  2026-09-28.
- [Ruff — Linter](https://docs.astral.sh/ruff/linter/) — "by default, Ruff will fix all violations for
  which safe fixes are available", with unsafe fixes gated behind `--unsafe-fixes` because they "could
  lead to a change in runtime behavior, the removal of comments, or both". The basis for splitting
  §14's local `--fix` run from the CI `ruff check .`. Checked 2026-08-20. Re-read 2026-09-28 for two
  facts the trim added. First, the reason in §14's CI comment. Under "Exit codes", `ruff check` exits
  `0` "if no violations were found, or if all present violations were fixed automatically", `1` "if
  violations were found", and `2` on abnormal termination. `--exit-non-zero-on-fix` makes it exit `1`
  even when every violation was fixed. So "rewrites the checkout and exits 0" holds when every
  violation in the commit is fixable. Unfixable ones still exit `1`. A run of ruff 0.16.9 the same day
  gave `Found 2 errors (2 fixed, 0 remaining)` and exit 0. Second, §5's `# noqa: ANN401`: "To ignore
  an individual violation, add `# noqa: {code}` to the end of the line".
- [Ruff — `complex-structure` (C901)](https://docs.astral.sh/ruff/rules/complex-structure/) —
  cyclomatic complexity is enforced by `C901` from the **mccabe** (`C90`) family, a different namespace
  from flake8-comprehensions (`C4`). This is why §14 names `C90` (optional) separately; without it,
  §3's complexity limit is unmeasured. The page states no default value for `max-complexity`, so none is quoted anywhere
  here. Checked 2026-08-20.
- [Ruff — `any-type` (ANN401)](https://docs.astral.sh/ruff/rules/any-type/) — the scope §5 gives the
  rule. It "checks that function arguments and return values are annotated with a more specific type
  than `Any`", so it covers parameters and returns only. `*args`/`**kwargs` are governed by
  `lint.flake8-annotations.allow-star-arg-any`, and a documented false positive fires on aliases of
  `Any` (`MyAny = Any`). The page does not say how an `Any` nested inside a larger annotation is
  treated. That part of §5 comes from a run of ruff 0.16.9 (`ruff check --isolated --select ANN401`)
  on 2026-09-28, recorded here as tool output rather than documentation. The run flagged `x: Any`,
  `-> Any` and an `x: Any | None` parameter. It did not flag `dict[str, Any]` or `list[Any]` in a
  parameter or return, a `y: Any` variable annotation, or `cast(dict[str, Any], ...)`. §5 now names
  the top-level `Any | None` case. A second run the same day, for the cross-skill review, also
  flagged `x: Optional[Any]` and a `-> Any | None` return, and again passed `dict[str, Any]` and
  `list[Any]`. Re-run the check when the pinned ruff changes. Checked 2026-09-28.
- [mypy — Command line (`--strict`)](https://mypy.readthedocs.io/en/stable/command_line.html) — the
  exact flag set behind `--strict` (thirteen flags, from `--disallow-any-generics` to
  `--extra-checks`), the statement that it is "a defined subset of optional error-checking flags", and
  the warning that "the exact list of flags enabled by running `--strict` may change over time". Also
  the negative fact §5 now states: no flag in the strict set bans explicit `Any`. mypy 2.3.1 docs,
  checked 2026-08-20. Re-read 2026-09-28 for §5's `disallow_any_explicit` clause, still mypy 2.3.1.
  `--disallow-any-explicit` "disallows explicit `Any` in type positions such as type annotations and
  generic type parameters". That covers variable annotations and a nested `Any` such as
  `dict[str, Any]`. The flag is still absent from the thirteen-flag `--strict` list. The page does not
  mention `cast`; the source entry below settles that.
- [mypy source — `mypy/checkexpr.py` at tag v2.3.1](https://raw.githubusercontent.com/python/mypy/v2.3.1/mypy/checkexpr.py)
  — the `cast` part of §5's claim, which the docs do not state. `visit_cast_expr` calls
  `check_for_explicit_any(target_type, ...)` on the cast's target type. That function, in
  `mypy/typeanal.py` at the same tag (also read), reports an error when `disallow_any_explicit` is set
  and `has_explicit_any(typ)` is true: "Whether this type is or type it contains is an Any coming from
  explicit type annotation". So `cast(dict[str, Any], x)` is an error under the flag.
  `mypy/checker.py` at the same tag (also read) makes the same call on `s.type` in
  `visit_assignment_stmt`, which covers annotated variables. Source code rather than documentation,
  so it can change without a docs note. Checked 2026-09-28.
- [mypy — Error codes enabled by default](https://mypy.readthedocs.io/en/stable/error_code_list.html)
  — `arg-type`, the code in §14's `# type: ignore[arg-type]` example, is real: "Check argument types
  [arg-type]. Mypy checks that argument types in a call match the declared argument types in the
  signature of the called function (if one exists)." mypy 2.3.1, checked 2026-09-28.
- [mypy — The mypy configuration file](https://mypy.readthedocs.io/en/stable/config_file.html) — that
  configuration may live in `pyproject.toml`, and `strict` = "enable all optional error checking
  flags", again with the may-change caveat. Backs §14's config location and version-pinning rule.
  mypy 2.3.1, checked 2026-08-20.
- [mypy — More types (`NewType`)](https://mypy.readthedocs.io/en/stable/more_types.html) — the contract
  §5's example previously broke: the string literal "must equal the name of the variable to which the
  new type is assigned", and you "cannot use `isinstance()` or `issubclass()` on the object returned by
  NewType, nor can you subclass" it (that second half trimmed from SKILL.md 2026-09-28). mypy 2.3.1,
  checked 2026-08-20.
- [Python documentation — `typing` (type aliases and `NewType`)](https://docs.python.org/3/library/typing.html)
  — the clause §5 added, that a PEP 695 `type` alias "does not stop IDs mixing". The `NewType`
  section says: "Recall that the use of a type alias declares two types to be *equivalent* to one
  another. Doing `type Alias = Original` will make the static type checker treat `Alias` as being
  *exactly equivalent* to `Original` in all cases." It adds that `NewType` instead makes the checker
  treat the new type as a subclass, so "a value of type `Original` cannot be used in places where a
  value of type `Derived` is expected". Page self-reports Python 3.14.7. Checked 2026-09-28.
- [mypy — Using mypy with an existing codebase](https://mypy.readthedocs.io/en/stable/existing_code.html)
  — context for §14's strict mandate: mypy advises incremental adoption, notes options "can be enabled
  on a per-module basis", singles out `disallow_untyped_defs` ("strongly recommend enabling this one as
  soon as you can"), and frames passing `--strict` as "an excellent goal to aim for" rather than a
  starting configuration. mypy 2.3.1, checked 2026-08-20.

**Testing**

- [pytest — How to parametrize fixtures and test functions](https://docs.pytest.org/en/stable/how-to/parametrize.html)
  — confirms §13's `@pytest.mark.parametrize` spelling and usage against the documented example. No
  error found. Docs "stable" (console output shows `pytest-9.x.y`), checked 2026-08-20.
- [pytest — How to mark test functions with attributes](https://docs.pytest.org/en/stable/how-to/mark.html)
  — the requirement §13 was missing: unregistered marks "will always emit a warning", and with the
  `strict_markers` configuration option set, unknown marks "will trigger an error". Checked 2026-08-20.
- [pytest — How to use fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html) — every
  fixture idiom §13 relies on: `@pytest.fixture`, sharing via `conftest.py`, `yield` fixtures for
  teardown (the mechanism behind transaction-rollback isolation), the scope list, and fixture
  parametrization via `params`. No error found. Checked 2026-08-20.
- [pytest-cov — Configuration](https://pytest-cov.readthedocs.io/en/latest/config.html) — `--cov` and
  `--cov-fail-under` are pytest-**cov** options, not pytest core ones, which is why §14 now says to
  declare the plugin. pytest-cov 7.1.0, checked 2026-08-20. Neither this page nor pytest recommends any
  coverage percentage; §13's 85% is a house floor.
- [pytest — How to capture warnings](https://docs.pytest.org/en/stable/how-to/capture-warnings.html)
  — §14's `filterwarnings = ["error"]`. The `filterwarnings` configuration option is the file form
  of `-W`, and the page's example, with `'error'` as the first entry, "will ignore all user warnings
  and specific deprecation warnings matching a regex, but will transform all other warnings into
  errors". Its examples use a `[pytest]` table (`pytest.toml` or `pytest.ini`); the `pyproject.toml`
  table name comes from the Configuration entry below. A pytest 9.1.1 run on 2026-09-28 with
  `[tool.pytest.ini_options] filterwarnings = ["error"]` failed a test that emitted a
  `DeprecationWarning`. The same runs exposed a trap for §13's `TestClient`. With starlette 1.7.0
  and `httpx` installed but not `httpx2`, importing `fastapi.testclient` emits
  `StarletteDeprecationWarning` ("Using `httpx` with `starlette.testclient` is deprecated; install
  `httpx2` instead"), so under this setting a `conftest.py` that imports `TestClient` fails
  collection. Page shows `pytest-9.x.y`. Checked 2026-09-28.
- [pytest — Configuration](https://docs.pytest.org/en/stable/reference/customize.html) — the table
  §13 and §14 put pytest settings under. `pyproject.toml` takes `[tool.pytest.ini_options]` "for
  INI-style configuration (supported since pytest 6.0)" or `[tool.pytest]` "to leverage native TOML
  types (supported since pytest 9.0)". SKILL.md uses the first, which pytest 9.1.1 still reads.
  Checked 2026-09-28.
- [PostgreSQL — CREATE INDEX](https://www.postgresql.org/docs/current/sql-createindex.html) — §13's
  reason migration tests get their own database: "a regular `CREATE INDEX` command can be performed
  within a transaction block, but `CREATE INDEX CONCURRENTLY` cannot". A transaction-rollback
  fixture runs each test inside a transaction block, so a migration using `CONCURRENTLY` fails under
  it. The "current" page documents PostgreSQL 18. A local PostgreSQL 14.20 run on 2026-09-28 gave
  "ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block" after `BEGIN` and
  succeeded outside one. Checked 2026-09-28.

**SQLAlchemy** (SQLAlchemy 2.0.52, checked 2026-08-20, except the one entry marked 2026-09-28)

- [ORM Quick Start](https://docs.sqlalchemy.org/en/20/orm/quickstart.html) — `DeclarativeBase`,
  `Mapped`, `mapped_column()` confirmed as the current 2.0 spelling for §9, and the docs' own
  `session.scalars(stmt)` execution idiom.
- [ORM Querying Guide — SELECT statements](https://docs.sqlalchemy.org/en/20/orm/queryguide/select.html)
  — both `session.execute(select(...))` and `session.scalars(select(...))` are documented, with
  `Session.scalars()` described as "the equivalent" of execute-then-scalars. Source of §9's corrected
  wording.
- [Session API](https://docs.sqlalchemy.org/en/20/orm/session_api.html) — `with session.begin():` as
  "begin a transaction, or nested transaction, on this Session, if one is not already begun". The
  commit-on-exit wording confirmed on this read was `sessionmaker.begin()`'s, so no precise
  commit-semantics quote for `Session.begin()` is attributed here.
- [ORM Querying Guide — Relationship Loading Techniques](https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html)
  — `lazy="raise"`, `lazy="raise_on_sql"`, `raiseload()`, `selectinload`, `joinedload` all exist as
  documented; and the caveat §9 now carries: the raiseload strategies "do not apply within the unit of
  work flush process". No sentence distinguishing `raise` from `raise_on_sql` could be retrieved, which
  is why §9 says only that they are not synonyms.
- [Configuring a Version Counter](https://docs.sqlalchemy.org/en/20/orm/versioning.html) — §9's
  `version_id_col` is current, declared via `__mapper_args__`, and a mismatch raises `StaleDataError`.
- [Selectable API](https://docs.sqlalchemy.org/en/20/core/selectable.html) — confirms
  `with_for_update()` exists on `Select` in 2.0. Existence only: the render truncated before the method
  docstring, so nothing about `nowait`/`read`/`of`/`skip_locked` is attributed to this reading.
- [Connection Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html) — the defaults §9 now
  states: `pool_size` 5, `max_overflow` 10, `pool_recycle` -1, `pool_pre_ping` as a checkout liveness
  ping; and which pool class each dialect uses (`QueuePool` by default, `SingletonThreadPool` for
  SQLite `:memory:`, `AsyncAdaptedQueuePool` under `create_async_engine`).
- [Engine Configuration](https://docs.sqlalchemy.org/en/20/core/engines.html) — the scoping sentences
  behind §9's pooling caveat: `max_overflow` "is only used with QueuePool"; `pool_size` is "used with
  QueuePool as well as SingletonThreadPool".
- [ORM-Enabled INSERT, UPDATE, and DELETE Statements](https://docs.sqlalchemy.org/en/20/orm/queryguide/dml.html)
  — §12's `session.execute(insert(Model), rows)`, which replaced the 2026-07-31 text's
  `bulk_insert_mappings`. Under "ORM Bulk INSERT Statements", an `insert()` "constructed in terms of
  an ORM class" and passed to `Session.execute()` with "a list of parameter dictionaries" will "invoke
  bulk INSERT mode for the statement". The page's example is
  `session.execute(insert(User), [{"name": ..., "fullname": ...}, ...])`. The keys "should match the
  ORM mapped attribute name and not the actual database column name", so `rows` means attribute-keyed
  dicts. "Changed in version 2.0" says this form "makes use of the same functionality as the legacy
  `Session.bulk_insert_mappings()` method". The "Legacy Session Bulk INSERT Methods" section says the
  old methods "lack many features, namely RETURNING support as well as support for
  session-synchronization", and ports `bulk_insert_mappings(User, [...])` to
  `session.execute(insert(User), [...])`. §12's `session.execute(update(...))` is on the same page,
  including the bulk-UPDATE-by-primary-key form. The page self-reports **2.0.54** (released
  2026-09-15) with a "legacy version" label beside the release number. The newer series that label
  implies was not opened in this pass, and §9 and §12 still target 2.0. Checked 2026-09-28.

**FastAPI** (docs pages carry no version stamp; read 2026-08-20 against fastapi 0.141.1, except
entries marked 2026-09-28, read that day against fastapi 0.141.1 and starlette 1.7.0)

- [Response Model / Return Type](https://fastapi.tiangolo.com/tutorial/response-model/) — the
  return-type-first framing §10 now follows, and `response_model=` as the documented escape hatch that
  takes priority when both are present. Re-read 2026-09-28 for §10's `null` rule. With
  `response_model_exclude_unset=True`, "those default values won't be included in the response, only
  the values actually set"; values set explicitly, even to `None` or to the default, "will be
  included in the JSON response". `response_model_exclude_none=True` is named as an alternative. A
  fastapi 0.141.1 run agreed: for `Out(a=1, c=None)`, with `b` and `c` defaulting to `None` and
  `d: int = 5`, `exclude_none` returned `{"a": 1, "d": 5}` and `exclude_unset` returned
  `{"a": 1, "c": null}`. So `exclude_unset` also drops a non-null member left at its default (`d`),
  not only absent ones.
- [Reference — FastAPI class](https://fastapi.tiangolo.com/reference/fastapi/) — confirms every
  response-shaping parameter SKILL.md names exists with that exact spelling: `response_model` and
  `status_code` (§10), `dependencies` (§8), and `response_model_exclude_none` (§10; dropped by the
  2026-09-28 trim and restored by the cross-skill review the same day).
- [Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/) — `Annotated[X, Depends(...)]` is
  the docs' recommended form ("prefer to use the `Annotated` version if possible"). §10 matches the
  source here.
- [Dependencies in Path Operation Decorators](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-in-path-operation-decorators/)
  — the router-level `dependencies=[Depends(...)]` pattern §8 mandates for cross-cutting auth,
  documented for dependencies whose return value you don't need. No conflict.
- [Dependencies with yield](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/)
  — the `try` / `yield` / `finally: close()` session shape (trimmed from SKILL.md 2026-09-28; §9 keeps
  only the `with Session(engine)` form), and the exception-propagation behaviour behind §9's
  transaction rule.
- [SQL (Relational) Databases](https://fastapi.tiangolo.com/tutorial/sql-databases/) — backs §10's
  separate In/Out schemas (clients cannot set `id`; `secret_name` is never returned), and is the
  pattern §9 departs from: the tutorial injects the session into the path operation and calls
  `select()` there, while §9 says routes never receive the session. Its `with Session(engine)` shape
  is the one §9 uses. It is also written against SQLModel, not plain SQLAlchemy.
- [Handling Errors](https://fastapi.tiangolo.com/tutorial/handling-errors/) — `@app.exception_handler`
  registration, overriding `RequestValidationError`, and raising rather than returning `HTTPException`.
  §1's ban on `HTTPException` in services is a layering preference the docs neither state nor
  contradict. Re-read 2026-09-28 for §10's `loc[0]` split: the page's default error bodies show
  `"loc": ["path", "item_id"]` and `"loc": ["body", "size"]`. The full set of first elements is in
  the `fastapi/params.py` entry below. A fastapi 0.141.1 run with no custom handler returned `422`
  for a path-only, a query-only and a body-only error, so the `400` half of §10's split is the house
  handler's, not FastAPI's default.
- [Query Parameter Models](https://fastapi.tiangolo.com/tutorial/query-param-models/) — §10's
  `extra="forbid"` query model. A Pydantic model declared as `Annotated[FilterParams, Query()]` with
  `model_config = {"extra": "forbid"}` answers `?tool=plumbus` with `"type": "extra_forbidden"`,
  `"loc": ["query", "tool"]`. "This is supported since FastAPI version 0.115.0." The page does not
  say what happens to unknown parameters without such a model. That half of §10 ("FastAPI ignores
  unknown query parameters") is from a fastapi 0.141.1 run on 2026-09-28: `GET /plain?foo=1&page_size=5`,
  on a route declaring only `page_size`, returned 200 with `page_size` 5. The same run showed
  `Query(le=100)` rejecting `page_size=500` as `less_than_equal` at `["query", "page_size"]`, the
  behaviour §10's clamp rule avoids. Checked 2026-09-28.
- [Path Operation Configuration](https://fastapi.tiangolo.com/tutorial/path-operation-configuration/)
  — §15's `summary` and `description` decorator parameters, shown as `summary="Create an item"` and
  `description="Create an item with all the information, ..."`. Checked 2026-09-28.
- [Additional Responses in OpenAPI](https://fastapi.tiangolo.com/advanced/additional-responses/) —
  §15's `responses={...}`. A `model` key is placed under `content`, in "A key with the media type,
  e.g. `application/json`", and the page's generated output files it under `application/json`. A
  problem body declared as `application/problem+json` therefore has to name that media type under
  `content` itself. The page does not say when FastAPI adds its default `422`; the
  `fastapi/openapi/utils.py` entry below does. Checked 2026-09-28.
- [Declare Request Example Data](https://fastapi.tiangolo.com/tutorial/schema-extra-example/) — §15's
  `Field(examples=...)`: "When using `Field()` with Pydantic models, you can also declare additional
  `examples`", given as a list (`Field(examples=["Foo"])`). Checked 2026-09-28.
- [Extending OpenAPI](https://fastapi.tiangolo.com/how-to/extending-openapi/) — §15's `app.openapi()`
  export: "A FastAPI application (instance) has an `.openapi()` method that is expected to return the
  OpenAPI schema", and `/openapi.json` "just returns a JSON response with the result". A fastapi
  0.141.1 run returned a `dict` with `"openapi": "3.1.0"`. Checked 2026-09-28.
- [FastAPI source — `fastapi/openapi/utils.py` at tag 0.141.1](https://raw.githubusercontent.com/fastapi/fastapi/0.141.1/fastapi/openapi/utils.py)
  — when FastAPI adds its default `422`, which the docs do not state. It writes the
  `HTTPValidationError` schema under `application/json` into `operation["responses"]["422"]` only
  when the route has parameters or a body and
  `not any(status in operation["responses"] for status in [http422, "4XX", "default"])`. An
  additional response given a `model` gets `media_type = route_response_media_type or
  "application/json"`. So the default `422` schema is replaced only when `422`, `4XX` or `default`
  is among the declared responses. A fastapi 0.141.1 run confirmed it: `responses={400: ..., 404:
  ...}` declared as `application/problem+json` kept the default `422` as `application/json`;
  declaring `422` or `4XX` replaced it; `{422: {"model": Problem}}` came out as `application/json`.
  The downloaded file is byte-identical to the installed 0.141.1 package. Source code rather than
  documentation, so it can change without a docs note. Checked 2026-09-28.
- [FastAPI source — `fastapi/params.py` at tag 0.141.1](https://raw.githubusercontent.com/fastapi/fastapi/0.141.1/fastapi/params.py)
  — the possible `loc[0]` values behind §10's `422`/`400` split. `ParamTypes` has exactly `query`,
  `header`, `path` and `cookie`. `fastapi/dependencies/utils.py` at the same tag (also read) builds
  a parameter error's `loc` as `(field_info.in_.value, ...)` and a body error's as `("body", ...)`.
  A fastapi 0.141.1 run sending bad path, query, header, cookie and body values got `loc[0]` of
  `path`, `query`, `header`, `cookie` and `body`. §10 assigns no status to `cookie`. The split itself
  follows the api-contract-design-best-practices skill (`400` for an invalid query parameter, `422`
  for body field validation). Checked 2026-09-28.
- [Starlette — Exceptions](https://starlette.dev/exceptions/) — the limit on §10's "only middleware
  sees the final status, headers, and body". The stack is "`ServerErrorMiddleware` - Returns 500
  responses when server errors occur", then "Installed middleware", then "`ExceptionMiddleware` -
  Deals with handled exceptions, and returns responses". Handled exceptions "are coerced into
  appropriate HTTP responses, which are then sent through the standard middleware stack"; errors
  "should bubble through the entire middleware stack as exceptions", and the handler for them "is
  `exception_handler[500]` or `exception_handler[Exception]`". So user middleware sees the final
  response of a route or of a non-`500` handler, but sees an unhandled error only as the raised
  exception. starlette 1.7.0's installed `applications.py` (`build_middleware_stack`) puts the
  `500`/`Exception` handler on `ServerErrorMiddleware` and the rest on `ExceptionMiddleware`. A run
  with a pure ASGI middleware confirmed it: a `DomainError` handler's `409` `application/problem+json`
  start message and body passed through the middleware; a `RuntimeError` reached it as the exception,
  while the client still got the catch-all's `500`. The api-contract-design-best-practices skill
  requires replaying a `5xx` produced once execution began, so replay middleware must record that
  case from the exception. The page shows no version. Checked 2026-09-28.
- [Starlette — Middleware](https://starlette.dev/middleware/) — the same order from the other side:
  `ServerErrorMiddleware` "is always the outermost middleware layer", and `ExceptionMiddleware` "Adds
  exception handlers". The old `www.starlette.io` host no longer resolves from this environment, and
  `www.starlette.dev` redirects to `starlette.dev`. Checked 2026-09-28.
- [Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) — confirms §10's lifespan rule
  exactly, including that lifespan is "the recommended way to handle the startup and shutdown" over
  the older event handlers. No conflict.
- [Concurrency and async / await](https://fastapi.tiangolo.com/async/) — the threadpool mechanic §10
  cites, and the warning against plain `def` for trivial compute-only path operations that §10 now
  incorporates.
- [fastapi on PyPI](https://pypi.org/project/fastapi/) — used only to pin the unversioned docs pages
  above to a release: fastapi 0.141.1, released 2026-07-29. The release index is not the documentation;
  `fastapi.tiangolo.com` remains the canonical home.

**Security** (all checked 2026-08-20, one re-read 2026-09-28 as marked; none of the cheat sheets displays a version or revision date)

- [OWASP Top 10:2025](https://owasp.org/Top10/2025/) — the risk taxonomy §8 implicitly organises
  itself around. Mapping: parameterized queries and output encoding (trimmed from SKILL.md 2026-09-28)
  → A05 Injection; authz and SSRF (§8) and IDOR (trimmed from SKILL.md 2026-09-28) → A01 Broken Access
  Control (the introduction page, `/Top10/2025/0x00_2025-Introduction/`,
  states SSRF "has been rolled into this category" — that sentence is there, not on the landing page
  linked above); password, JWT and token rules (§8) → A07 Authentication Failures; TLS (§8) and
  encryption at rest (trimmed from SKILL.md 2026-09-28) → A04 Cryptographic Failures; CORS and headers
  (§8) and `DEBUG=False` (trimmed from SKILL.md 2026-09-28) → A02 Security Misconfiguration; lockfile
  and `pip-audit` (§14) → A03 Software Supply Chain Failures; generic error responses (trimmed from
  SKILL.md 2026-09-28) → A10 Mishandling of Exceptional Conditions. This mapping is a reconstruction:
  §8 named no edition and no artifact. Note the gaps it exposes — nothing here corresponds to A06
  Insecure Design or A08 Software or Data Integrity Failures, and A09 is only partly covered even after
  §7's security-event rule.
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) — the umbrella. This series, not any
  document called "OWASP Security Guidelines", is what actually supplies §8's prescriptive rules; cite
  the individual sheets below rather than the index.
- [Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
  — §8's hashing rule: the Argon2id → scrypt → bcrypt order of preference with parameters, the
  PBKDF2-HMAC-SHA-256 ≥ 600,000 carve-out for FIPS-140, and the narrower real objection to fast hashes
  ("they allow attackers to perform large numbers of guesses quickly"; the bare-fast-hash ban itself
  was trimmed from SKILL.md 2026-09-28). The rendered page shows no
  revision date; the upstream markdown was last committed 2026-06-24, i.e. before this document was
  written, so the divergence corrected in §8 is not staleness. Re-read 2026-09-28 for §8's second
  Argon2id setting. The 2026-07-31 text attributed that setting to ASVS and said "take the stricter";
  §8 now gives it as an equal alternative. The cheat sheet lists five recommended configurations:
  m=47104 (46 MiB), t=1, p=1 and m=19456 (19 MiB), t=2, p=1 (each marked "Do not use with Argon2i"),
  then m=12288/t=3, m=9216/t=4 and m=7168/t=5, all with p=1. It says "These configuration settings
  provide an equal level of defense, and the only difference is a trade off between CPU and RAM
  usage". So §8's "m ≥ 19 MiB, t = 2, p = 1, or m ≥ 46 MiB, t = 1, p = 1" is two of the cheat sheet's
  own equal-strength options. §8 omits the three lower-memory ones.
- [SQL Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)
  — the injection rule and "parameterized always" (both trimmed from SKILL.md 2026-09-28): prepared
  statements with parameterized
  queries first, allow-list input validation, escaping "strongly discouraged", and table and column
  names coming from code rather than user parameters. No conflict.
- [Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
  — the credential-policy bullet added to §8: length floors, "maximum password length should be at
  least 64 characters", "there should be no password composition rules", the failed-login counter bound
  to the account rather than the source IP, constant-time comparison, and generic login errors.
- [Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
  — §8's deny-by-default and object-level checks: "perform access control checks on every request for
  the specific object or functionality being accessed". No conflict.
- [File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
  — "list allowed extensions", and the reason §8's upload rule was rewritten: the `Content-Type` "is
  provided by the user, and as such cannot be trusted, as it is trivial to spoof".
- [Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) — §7's
  never-log list (session identifiers, access tokens, passwords, connection strings, keys, PII), the
  correlation-ID rule, and the security-event list §7 previously omitted.
- [Deserialization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html)
  — the ban on `pickle.loads` over untrusted data (trimmed from SKILL.md 2026-09-28) (the page also
  names PyYAML `load` and jsonpickle, which SKILL.md never did). No conflict.
- [Server Side Request Forgery Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
  — §8's outbound allow-list ("deny-lists are bypass-prone; prefer allow-lists"), plus two controls
  §8 still omits: disabling redirect-following, and re-resolving A/AAAA records against DNS rebinding.
- [Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)
  — the rotation requirement and the secret-manager-first order (both trimmed from SKILL.md 2026-09-28),
  and the divergence SKILL.md used to state: environment variables
  are rated a fallback, "not recommended unless the other methods are not possible". It does not
  mention `.env` files, so "never commit `.env`" (trimmed from SKILL.md 2026-09-28) was a house rule.
- [OWASP ASVS — project page](https://owasp.org/www-project-application-security-verification-standard/)
  — establishes the current release: ASVS 5.0.0, 30 May 2025.
- [OWASP ASVS 5.0.0 (standard text)](https://github.com/OWASP/ASVS/tree/v5.0.0/5.0/en) — the testable
  layer behind §8: V5.2.2 and V5.1.1/V5.2.1 (upload extension-plus-content and size limits), V6.2.1
  and V6.2.9 (password length), Appendix C (the approved hash parameters — Argon2id, scrypt, bcrypt
  cost ≥ 10, PBKDF2 iteration floors), V3.4.1–V3.4.8 (CORS allowlist and the specific security
  headers), and V16.3.1/V16.3.2/V16.3.4 plus V16.5.1 (log authentication and authorization outcomes,
  now §7; generic error messages, trimmed from SKILL.md 2026-09-28). Chapters read: V3, V5, V6, V16, Appendix C. Note the algorithm parameters
  live in Appendix C, not in V6 — do not cite "ASVS V6" for them. Note also a real disagreement
  between two OWASP artifacts this document cites side by side: Appendix C's approved argon2id setting
  is "t = 1: m ≥ 47104 (46 MiB), p = 1", while the Password Storage Cheat Sheet's minimum is
  m = 19456 (19 MiB), t = 2, p = 1. §8 lists both settings as alternatives; nothing in this document
  attributes an argon2id figure to Appendix C. The 2026-09-28 re-read of the cheat sheet (entry above)
  narrows that disagreement. The cheat sheet's own first-listed option is m=47104 (46 MiB), t=1, p=1,
  rated equal to its 19 MiB, t=2 option. So both artifacts accept the 46 MiB, t=1 setting. On the
  2026-08-20 reading of Appendix C, the disagreement is only over whether 19 MiB, t=2 (and the cheat
  sheet's lower-memory options) also qualify. Appendix C itself was not re-read on 2026-09-28.

**Architecture**

- [Robert C. Martin — The Clean Architecture (2012-08-13)](https://blog.cleancoder.com/uncle-bob/2012/08/13/the-clean-architecture.html)
  — the only public canonical statement of Clean Architecture, and the source of "dependencies point
  inward" and the layer table (both §1): the Dependency Rule ("source code dependencies can only point
  inwards. Nothing in an inner circle can know anything at all about something in an outer circle"),
  the circles being schematic, "the Web is a detail. The database is a detail", and the
  boundary-crossing rule about simple data structures and not passing Entities or database rows — which
  §1's "services may receive ORM objects" rule deliberately departs from. Checked 2026-08-20.
- [Robert C. Martin — The Single Responsibility Principle (2014-05-08)](https://blog.cleancoder.com/uncle-bob/2014/05/08/SingleReponsibilityPrinciple.html)
  — the SRP wording (trimmed from SKILL.md 2026-09-28): "each software module should have one and only one reason to change", "this
  principle is about people", and the actor framing this document previously dropped. Checked
  2026-08-20.
- [Robert C. Martin — Solid Relevance (2020-10-18)](https://blog.cleancoder.com/uncle-bob/2020/10/18/Solid-Relevance.html)
  — the citable public enumeration of all five SOLID principles in the author's own words. Read
  primarily to establish what this document does *not* use: only dependency inversion (§5's Protocol
  ports) appears anywhere in it; SRP was trimmed from SKILL.md 2026-09-28. Checked 2026-08-20.
- [Clean Architecture: A Craftsman's Guide to Software Structure and Design — Robert C. Martin (Pearson, 2017)](https://www.informit.com/store/clean-architecture-a-craftsmans-guide-to-software-structure-9780134494166)
  — **a bibliographic entry, not a rule source.** The publisher's page was read for author, publisher,
  edition (1st, 10 September 2017) and ISBN-13 978-0-13-449416-6, on 2026-08-20. The book itself was
  not read. No rule, threshold, or wording in this document may be attributed to a chapter or page of
  it; where the architecture rules here can be sourced at all, they are sourced to the 2012 blog post
  above.
- [Domain-Driven Design: Tackling Complexity in the Heart of Software — Eric Evans (Addison-Wesley, 2003)](https://www.informit.com/store/domain-driven-design-tackling-complexity-in-the-heart-9780321125217)
  — **a bibliographic entry, not a rule source.** Publisher page read 2026-08-20 for author, publisher,
  edition (1st, 20 August 2003) and ISBN-13 978-0-321-12521-7. The book was not read; attribute no rule
  to a page or chapter of it. Use the DDD Reference below for any DDD wording.
- [Domain-Driven Design Reference: Definitions and Pattern Summaries — Eric Evans (2015-03, CC BY 4.0)](https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf)
  — the only primary DDD text actually read (downloaded and text-extracted 2026-08-20: 59 pages,
  © 2015 Eric Evans, "Creative Commons Attribution 4.0 International License" per the title page): the
  author's own summaries of Layered Architecture, Entities, Value Objects, Aggregates, Repositories,
  Services and Modules. It supplies the quotations behind the DDD divergences whose rules are now in
  §1, §9 and §12 (the quotations themselves trimmed from SKILL.md 2026-09-28), and each of those was
  checked word-for-word against the extracted text. By construction it is a
  summary rather than an argument — the subtitle is "Definitions and Pattern Summaries", and the
  acknowledgements describe the contents as the "brief summaries of each pattern" extracted from the
  2004 book — so it can settle what a pattern *says* but not the book's surrounding reasoning. It
  carries no disclaimer beyond that: it contains no sentence limiting its own scope, so do not quote
  one. Retrieval note: this host answers some automated fetchers with HTTP 403, so the PDF was pulled
  directly (HTTP 200, 484 KB) and text-extracted rather than read through a page-fetch tool.

**What rests on general practice, not on any source above**

The numeric thresholds are house rules. "Line length 100" (§14) matches no value in any cited source;
"≤ 4 parameters" and "cyclomatic complexity ≤ 8" (§3), "class over ~200 lines or >7 public methods" and
"inheritance depth ≤ 2" (§4), "~10 files per layer" (§1), "coverage ≥ 85%" (§13), and "route handler
≤ 15 lines" (§10) appear in none of them. Several resemble figures from Martin's *Clean Code*, a
different book that is not cited here and was not read. "Composition over inheritance" (trimmed from
SKILL.md 2026-09-28) is not a SOLID principle and comes from none of these sources; its usual
provenance is the Gang of Four, which this document does not cite.

Also general practice, stated from mechanics rather than quoted: §2's boolean prefixes and
units-in-name rule, the singular-module rule and enum-member convention (trimmed from SKILL.md
2026-09-28), and §13's test-name pattern; §6's exception-hierarchy
shape and retry guidance; §11's async rules as a whole (no page from `docs.python.org`, `httpx` or
`asyncio` was read in this pass; the one `docs.python.org` page read on 2026-09-28 is the `typing`
page, which supports only §5); "index every foreign key" and the expand/migrate/contract
sequence (trimmed from SKILL.md 2026-09-28), and §9's Alembic rules — Alembic is a separate project
from SQLAlchemy and its documentation was not checked; the pagination bounds (trimmed from SKILL.md
2026-09-28); §12's caching and streaming rules; §13's mocking and edge-case guidance; and §1's specific
layer prohibitions. The `text("... WHERE id = :id")` bound-parameter spelling (trimmed from SKILL.md
2026-09-28) was not checked against SQLAlchemy's textual-SQL documentation in this pass.

The 2026-09-28 trim added two house rules that no source above states. One is §14's rule for a repo
not yet clean under the tools; the cross-skill review the same day narrowed it from changed files to
touched lines, and only its `ruff format --range` mechanics are sourced (Configuring Ruff entry
above). The other is §16's public-Python-API guardrail (keep a re-export or deprecation shim). Both
were restored from the 2026-07-31 agent rules ("no reformatting untouched files", "preserve backward
compatibility"), which were house rules too.

The cross-skill review also added house rules that the sources above support only in their
mechanics: §9's once-per-batch commit, §8's unauthenticated health probes, §10's `Idempotency-Key`
replay in middleware, and §13's separate migration-test database. §10's `422`-body/`400`-elsewhere
split, its unknown-query-parameter rejection, its `page_size` clamp, and omitting absent members
rather than emitting `null` come from the api-contract-design-best-practices skill, which owns those
rules and their sources.

Two named influences could not be verified and support nothing here. Martin's 2000 paper "Design
Principles and Design Patterns", where the principles later acronymised as SOLID first appeared, could
not be retrieved on 2026-08-20 — objectmentor.com no longer serves it and the Internet Archive was
unreachable from this environment — so no URL for it is given and nothing rests on it. And "OWASP
Security Guidelines", named in the original framing, is not the title of any OWASP document; §8 is now
attributed to the specific artifacts above (Top 10:2025, nine Cheat Sheet Series pages, ASVS 5.0.0),
reconstructed after the fact rather than recorded at authoring time.
