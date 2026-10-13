# Stage: plan

**Reads:** `task.md`, `profile.md`, `analysis.md` · **Writes:** `plan.md` (template: `templates/plan.md`)
**Restrictions:** read-only on the codebase. Produce no code changes; writing `plan.md` is the
one file write this stage performs.
**Budget:** at most 15 tool calls, to confirm details the analysis left open.
**Reasoning demand:** high. This is the hardest thinking in the loop, and a wrong plan is the most
expensive error available — every later stage faithfully executes it before anyone notices.

Read all three inputs.

## Job

Produce a plan that whoever implements next can execute **without seeing any of your reasoning**.
In isolated mode that is literally true; in sequential mode treat it as true anyway, because the
plan file is the artifact that survives and the reasoning is not.

The plan must carry your decisions *and the rationale behind them*. An implementer who gets only
conclusions will re-derive the choices underneath them, and contradict them.

Prefer reusing what `analysis.md` found over building new code. If you reject a found utility, say
why in the rationale.

## Output

1. **Decisions & rationale** — each significant choice and why, including rejected alternatives
   worth noting.
2. **Files to change** — every file, with a one-line description of the change.
3. **Steps** — ordered, concrete, small enough to verify individually.
4. **Out of scope** — what this task deliberately does not touch.
5. **Runnable check** — REQUIRED. An executable command (test invocation, build, script — taken
   from or consistent with `profile.md`) that passes if and only if the task is done. A plan
   without a runnable check is invalid and will be rejected.

On the runnable check: a command that always passes is worse than no check, because it terminates
the loop with a false PASS. If the project has no test harness, the check may be a script you
specify for the implementer to write — but then say so under **Files to change**.

If part of the acceptance criteria cannot be automated (needs a human, or would mutate a live
environment), do not silently drop it: list it under a **Manual checks** line in the same section.
The verifier will not run those — it reports them as unverified, so the final PASS stays honest
about its scope.

## Return

At most 10 lines: the shape of the plan, the runnable check command, and the path to `plan.md`.
