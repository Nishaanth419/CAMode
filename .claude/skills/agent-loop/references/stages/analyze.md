# Stage: analyze

**Reads:** `task.md`, `profile.md` · **Writes:** `analysis.md` (template: `templates/analysis.md`)
**Restrictions:** read-only on the codebase. Never edit code. Writing `analysis.md` is the one
file write this stage performs. Shell use is limited to read-only commands
(`git log`, `git grep`, `ls`, listing dependencies) — nothing that mutates files or state.
**Budget:** at most 15 tool calls of exploration.
**Reasoning demand:** moderate. The searching is mechanical; recognising that an existing utility
*already solves* the task is not. A model that misses it defeats the stage's whole purpose.

Read both inputs before exploring.

## Job

Find what the codebase already provides for this task, so downstream stages reuse instead of
re-implement. Most agent failures on real codebases come from rebuilding something that exists —
you are the gate that prevents that.

Prioritize, in order: existing utilities and services related to the task; the module(s) the change
will touch; tests covering that area; and one representative file per pattern the implementer
should imitate.

## Output

All four sections of the template are mandatory:

1. **Found** — existing utilities, functions, patterns relevant to the task, each with a file path.
2. **Exemplars** — one representative file per pattern the implementer should follow (e.g. "new
   routes should look like `src/routes/users.ts`").
3. **Missing** — what genuinely does not exist and must be built.
4. **Reuse plan** — one short paragraph: how the task should lean on what was found.

If the task touches code you could not locate, say so explicitly in **Missing** — never record that
something doesn't exist without having searched for it.

## Return

At most 10 lines: the headline findings and the path to `analysis.md`. All detail belongs in the
artifact, not in your reply.
