# Stage: implement

**Reads:** `profile.md`, `task.md`, and — when the tier produced them — `analysis.md`, `plan.md`
**Writes:** `implementation.md` (template: `templates/implementation.md`) plus the code changes
**Restrictions:** may modify source code freely (the debug stage inherits the same permission) —
but the development environment is not scratch space. Databases, running services, and seeded data
are real project state: remove any state you create for a manual check, restore anything you
rebuilt or restarted, and record every environment side effect under Evidence.
**Reasoning demand:** moderate. With a good plan and named exemplars, most of the difficulty has
already been removed upstream.

These artifacts are your entire context. You do not see any earlier stage's reasoning, and that is
by design.

## Job

- Follow `plan.md` step by step when it exists; deviate only when the code forces you to, and
  record every deviation.
- Imitate the exemplar files named in `analysis.md` — match the codebase's existing patterns,
  naming, and idioms rather than inventing your own.
- Reuse what analysis found. Do not build something listed under **Found**.
- Keep the diff minimal: no drive-by refactors, no changes outside the plan's scope.

## Verification is part of implementation

Before you finish, run the plan's **Runnable check** yourself (for tier-S tasks with no plan, run
the project's test command from `profile.md`). Fix what it surfaces.

Evidence means actual command output, not a claim that it passed. The verify stage will run the
same check independently, so a claim you cannot back will simply become a FAIL one stage later.

## Output

1. **What changed** — files and a one-line summary each.
2. **Deviations from plan** — each with the reason (or "none").
3. **Evidence** — the check command you ran and its actual output, trimmed to the relevant tail.

## Return

At most 10 lines: what changed, check result, and the path to `implementation.md`. Do not paste the
diff or full logs into your reply.
