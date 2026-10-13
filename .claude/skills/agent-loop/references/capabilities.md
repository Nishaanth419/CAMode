# Host capabilities

What the loop needs from the agent running it, and how it degrades when a capability is missing.
Read this once at the start of a run to pick your execution mode.

## Required

The loop cannot run without these. Every general-purpose coding agent has them.

- **Read files** — stages consume their inputs as files.
- **Write files** — stages produce artifacts as files.
- **Run shell commands** — the profile stage verifies commands, and the verifier must actually
  execute the runnable check rather than reason about it.
- **Search the codebase** — grep or equivalent, for the analyze stage.

If shell execution is unavailable, stop and tell the user: a loop whose verification cannot run
real commands has no termination condition, and running it anyway produces confident, unverified
output — the exact failure the design exists to prevent.

## Optional, in preference order

Each of these makes the loop stronger. None is mandatory; each has a stated fallback.

### Subagents with isolated contexts

**Best case.** Each stage runs in its own context window and returns only a short summary, so
exploration transcripts, diffs, and test logs never accumulate in the orchestrator.

- *Mode A.* Spawn one subagent per stage.
- *Fallback (Mode B).* Run the stages yourself in sequence. Read each stage contract, follow it,
  write the artifact, then move on — treating the artifact as the only thing that crosses the
  boundary. Discipline replaces isolation: when a contract lists its inputs, those are the *only*
  inputs, even if you happen to remember more.

### Per-agent tool restriction

Lets the verifier be spawned without file-write tools. This is defence in depth, not the
guarantee: the verifier always holds a shell (it must execute the runnable check), and a shell can
edit files, so withholding write tools removes the convenient path and states intent rather than
preventing mutation. The actual no-edit guarantee is the **diff fingerprint** in `SKILL.md` Step 2
— capture `git diff` before verification and again after, void the verdict if the tree changed —
and it is mandatory on every run, with or without tool restriction.

Tool restriction also works per tool, not per file, so a stage stripped of write tools normally
still produces its artifact through the shell (output redirection); only a stage that cannot write
by any means returns the full content instead, and the orchestrator persists it verbatim to the
artifact path (`SKILL.md`, artifact persistence) — the artifacts are the audit log, and a host
restriction must not create a gap in it.

- *Fallback.* None needed for the no-edit rule — the fingerprint runs regardless. A host without
  tool restriction loses only the defence-in-depth layer, not the guarantee.

### Per-stage model selection

Stages have different reasoning demands, declared in each stage contract. Hosts that can set a
model or a reasoning-effort level per delegated agent can spend strong-model budget on plan,
verify and debug while running profile, analyze and implement mid-tier.

- *Fallback.* Run every stage on the strongest model available. This is a pure cost difference —
  no guarantee in the loop depends on model variation, and a uniformly strong loop is strictly
  safer than a badly tuned one.
- *Caution.* The cheapest tier is a false economy on the analyze stage, whose value is noticing
  that something already exists. Missing that is the single most expensive failure the loop is
  built to prevent, and it costs far more than the model saved.

### Context clearing or compaction between stages

Approximates fresh contexts in Mode B and keeps the orchestrator small.

- *Fallback.* Keep artifacts terse and never quote them back into the conversation. The 10-line
  summary limit exists for this reason.

### Turn or step caps on a delegated agent

Bounds the debugger's spend mechanically.

- *Fallback.* The debug stage contract states its own cap; honour it by counting iterations. The
  3-iteration ceiling on the debug *loop* is enforced by the orchestrator either way, and that is
  the cap that actually matters.

### Declarative agent definitions

Hosts that support persistent agent definitions (a file per role, with its own tool allowlist) can
install the stage contracts as native agents rather than reading them per run. This is a packaging
convenience, not a behavioural difference — see `adapters/`.

- *Fallback.* Read the contract from `references/stages/` at the point of use. This is the default
  path and works everywhere.

## Declaring your mode

State the chosen mode in your first message of a run, next to the tier:

> Tier M, Mode B (no subagents available — running stages sequentially with diff-fingerprint
> verification).

The user should never have to guess which guarantees are structural and which are behavioural on
their platform.
