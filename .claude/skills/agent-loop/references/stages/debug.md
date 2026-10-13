# Stage: debug

**Reads:** `plan.md` (or `task.md`), `test-report.md`, `implementation.md`, the iteration number,
and the current diff. **Writes:** appends to `implementation.md`, plus code changes.
**Restrictions:** may modify source code, within the plan's scope only. The development
environment is not scratch space: remove any state you create while reproducing, and record every
environment side effect (seeded data, rebuilt or restarted services) in your report.
**Budget:** at most 25 turns per iteration. The orchestrator caps the loop at 3 iterations.
**Reasoning demand:** high. Root-causing is reasoning-heaviest, and a weak model here reaches for
the symptom suppressions this contract forbids.

You are here because the verifier returned FAIL. You start fresh on purpose: previous fix attempts
are not in your context, so they cannot bias you. The findings in `test-report.md` are your work
order.

## Job

For each finding:

1. **Reproduce it first.** Run the failing check and see it fail before touching code. A fix for a
   failure you never observed is a guess.
2. **Diagnose the root cause.** Trace the failure to the actual defect.
3. **Fix minimally**, staying inside the plan's scope.
4. **Re-run the check** and confirm it passes.

## Forbidden fixes

Do not suppress symptoms to turn a check green. Specifically: no skipping or deleting tests, no
loosening assertions, no widening types to silence a checker, no catching-and-ignoring errors, no
special-casing the test's inputs. These make the loop terminate while leaving the defect in place,
which is worse than an honest FAIL — the loop's entire value is that PASS means something.

## When the plan is wrong

If a finding reveals the *plan itself* is wrong rather than the implementation, stop and say so in
your report instead of forcing the code to match a broken plan. That is an escalation to the human,
not a code change, and the orchestrator will handle it.

## Output

Append to `implementation.md` under a `## Debug iteration N` heading: the root cause of each
finding, the fix, and the check output as evidence.

## Return

At most 10 lines: root cause(s), what you changed, check result. If you concluded the plan is at
fault, lead with that.
