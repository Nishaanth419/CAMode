# Stage: profile

**Reads:** the project itself · **Writes:** `.agent-loop/profile.md` (template: `templates/profile.md`)
**Restrictions:** read-only on source code. Shell use is permitted and required — but only for
commands that inspect or build, never ones that mutate tracked files or push state anywhere.
**Runs:** once per project, then cached. Refresh by deleting the file.
**Reasoning demand:** moderate. Detection is mechanical, but the exemplars you pick steer every
later stage, so this is not a stage to run on the weakest model available.

You are establishing the project-specific knowledge the rest of the loop depends on. Everything
downstream is generic; this file is where a particular codebase's reality lives.

## Job

1. **Read the project's own instructions first** — `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/`,
   `README`, `CONTRIBUTING`. If they state the test/build/lint commands, prefer them over inference.
   Record the paths of the instruction/standards files you found: the verifier judges the diff
   against them, so they must be a first-class input, not ambient knowledge.
2. Otherwise detect: package manager, test command, build command, lint/typecheck command.
3. **Run each discovered command once and confirm it works.** This is the point of the stage. A
   command that was guessed from a manifest and never executed will fail in the verify stage, where
   the failure looks like a broken implementation instead of a broken profile.
4. Identify one exemplar file per major pattern the implementer might need to imitate.
5. Note conventions and gotchas an agent could not infer from reading the code — including any
   live state later stages must not treat as scratch space (a development database, a running
   compose stack).

## Recording failures

If a command does not exist or does not work, write that down rather than inventing a substitute:

> **Lint:** none configured (`ruff` in `pyproject.toml` but not installed; `uv run ruff check .`
> exits 127)

A profile that honestly says "no test command" lets the planner design a runnable check that
actually runs. A profile that guesses one breaks every later stage silently.

## Return

At most 10 lines: the three commands and their verified status, the stack, and the path to
`profile.md`.
