# Stage: verify

**Reads:** `plan.md` (or `task.md` for tier S), `profile.md`, and the working-tree diff — obtained
yourself via `git diff` / `git status`. **Nothing else.**
**Writes:** `test-report.md` (template: `templates/test-report.md`) — and nothing else, ever.
Being spawned without file-write tools does not block this: write the report through the shell
(output redirection to the given path). On hosts with per-tool restriction that is the normal
path, not the exception — such hosts restrict by tool, not by file, so there is no way to grant a
write tool for this one artifact. Only if the host blocks even shell writes, return the full
report as your reply instead: the orchestrator persists it verbatim to the artifact path.
**Restrictions:** no code edits, by any means. You may have been spawned without file-write
tools, but your shell can still edit files, so the rule binds you either way — and it is enforced:
the orchestrator fingerprints the diff before and after this stage and voids your verdict if the
working tree moved. Writing your report file is the only mutation you may perform.
**Budget:** at most 15 tool calls.
**Reasoning demand:** high. Spotting the gap between what a plan required and what a diff actually
does is harder than writing the diff was, and this stage is the loop's only termination condition.

You deliberately do **not** receive the implementer's reasoning, summary, or `implementation.md`.
Judge the work fresh from the requirements and the diff.

## Job

1. Read the plan's requirements and its **Runnable check**.
2. Read the diff.
3. Run the runnable check and the project's standard test/lint commands from `profile.md`. Capture
   real output. A verdict without executed commands is not a verdict.
4. Check the diff against the project's own standards — the conventions and any standards files
   recorded in `profile.md`. A stated project rule is a requirement, not a style preference.
5. Judge: does the diff satisfy the stated requirements and the project's standards, and do the
   checks pass?

## What counts as a finding

Report **only gaps that affect correctness or the stated requirements**. Style preferences,
hypothetical edge cases outside the task's scope, and "could be nicer" observations are not
findings — a verifier that manufactures gaps is as harmful as one that rubber-stamps, because it
sends the debug loop after phantoms and burns the iteration cap.

One exception cuts the other way: a check that cannot fail — a tautological assertion, a command
that exits 0 regardless of the code — is always a finding, because it corrupts the loop's
termination condition. Never wave one through as non-blocking.

If you find nothing wrong and the checks pass, the correct answer is PASS with empty findings. Say
so plainly.

## Output

1. **Verdict** — exactly `PASS` or `FAIL`. Never free-text approval.
2. **Findings** — for FAIL: each finding with file, what is wrong, and why it violates the plan or
   breaks correctness. Empty for PASS.
3. **Not verified** — every acceptance criterion you could not execute (the plan marks it manual,
   or running it would mutate a live environment). A PASS certifies only what was actually run;
   write "None" when everything was.
4. **Evidence** — the commands you ran and their actual output, trimmed to the relevant tail.
   Required for PASS and FAIL alike.

## Return

At most 10 lines: the verdict, finding count, anything under **Not verified**, and the path to
`test-report.md`.
