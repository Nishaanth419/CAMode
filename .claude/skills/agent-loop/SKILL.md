---
name: agent-loop
description: Run a coding task through a staged loop — triage, analyze, plan, implement, verify, debug — with file-based handoff between stages, a fresh context per stage, and hard iteration caps. Use only when the user explicitly invokes the loop by name (for example "/agent-loop <task>" or "run the agent loop on this"); it is deliberately heavier than a normal edit and should never be auto-selected for ordinary requests.
license: MIT
metadata:
  version: "0.2.0"
disable-model-invocation: true
---

# Agent loop

You are orchestrating a staged pipeline for the task the user gave you.

**Your context must stay small.** Hold only: the task, the tier, the current stage, and artifact
file paths. Never paste artifact contents, diffs, or logs into your own context — stages
communicate through files, and each stage returns at most a 10-line summary.

This protocol is host-agnostic. Before Step 0, read `references/capabilities.md` once and
determine which **execution mode** applies to you:

| Mode | When | How stages run |
|---|---|---|
| **A — Isolated** | You can spawn subagents with their own context windows | One subagent per stage |
| **B — Sequential** | You cannot spawn subagents | You run each stage yourself, in order, reading only that stage's declared inputs |

Both modes follow the same contracts and gates. Mode B is not a lesser version of the loop —
file-based handoff is what makes the stages separable, and it works with one context or six.
State your mode alongside your tier decision in Step 1 so the user knows what they are getting.

## Step 0 — Run directory and profile

1. Create the run directory `.agent-loop/runs/<YYYY-MM-DD>-<short-slug>/` (slug: 2–4 words from
   the task). Ensure `.agent-loop/` is listed in `.gitignore`; add it if missing.
2. **Profile.** If `.agent-loop/profile.md` exists, use it. Otherwise run the **profile** stage
   (`references/stages/profile.md`) and write `.agent-loop/profile.md`. An unverified test command
   poisons every downstream stage, so the profile stage must execute each command it discovers.
3. Write `task.md` in the run directory: the task verbatim, plus your tier and mode decisions and
   the reasoning behind them (after Step 1).

## Step 1 — Triage

Classify the task yourself, inline — never delegate this. State the tier and a one-sentence reason
before proceeding, so a wrong sizing is visible and correctable.

| Tier | Signal | Pipeline |
|------|--------|----------|
| **S** | Diff describable in one sentence; single file; no design choice | implement → verify |
| **M** | Localized change, but touches unfamiliar code or needs a reuse check | analyze → implement → verify |
| **L** | Multi-file, new feature, architectural choice, or uncertain scope | analyze → plan → implement → verify |

When in doubt between two tiers, pick the smaller one — escalating mid-run is cheap (run the
skipped stage), while running the full pipeline on a trivial task is pure waste.

## Step 2 — Run the stages

Run the tier's stages in order. Each stage's contract is a file in `references/stages/`; each
stage's output artifact follows the matching template in `references/templates/`.

| Stage | Contract | Output artifact | Budget | Reasoning demand |
|---|---|---|---|---|
| profile | `references/stages/profile.md` | `profile.md` | — | moderate |
| analyze | `references/stages/analyze.md` | `analysis.md` | ≤ 15 tool calls | moderate |
| plan | `references/stages/plan.md` | `plan.md` | ≤ 15 tool calls | **high** |
| implement | `references/stages/implement.md` | `implementation.md` | — | moderate |
| verify | `references/stages/verify.md` | `test-report.md` | ≤ 15 tool calls | **high** |
| debug | `references/stages/debug.md` | appends to `implementation.md` | ≤ 25 turns | **high** |

**Mode A:** each stage is one subagent spawn, run in the foreground, in order. The delegation
prompt must contain: the stage's objective; the **absolute path to its stage contract**; absolute
paths to its input artifacts; the absolute path for its output artifact and template; its budget;
and the instruction to return at most 10 lines. Vague delegation causes duplicate work and gaps —
always pass the full brief. If your host lets you restrict a subagent's tools, apply the
restrictions each stage contract names.

**Mode B:** announce the stage, read its contract, and follow it as written — including its input
list. Do not carry forward context a stage is not entitled to (see the verify gate below). If your
host can clear or compact context between stages, do so at each boundary.

### Model selection

Stages differ in how hard they are, so they do not all need the same model. If your host can choose
a model (or a reasoning-effort level) per stage, use each contract's **reasoning demand**:

- **high** — plan, verify, debug. Use the strongest model you have. A wrong plan is executed
  faithfully by every stage after it; a lazy verify is the loop's only termination condition
  failing open; a weak debug reaches for the symptom suppressions its contract forbids. These three
  are where quality has to come from.
- **moderate** — profile, analyze, implement. A mid-tier model is enough. Most of the difficulty
  has been removed upstream by the time these run.

Do not push the moderate stages to your cheapest model without measuring. Analyze in particular
looks mechanical but is not: its job is recognising that something *already exists*, and a model
that misses it hands the implementer a green light to rebuild it. That costs far more than the
model saved.

If your host cannot vary the model, run the whole loop on the strongest one available and skip this
section. Quality is unaffected; only cost is.

**Artifact persistence:** a stage that cannot write its artifact (a host tool restriction)
returns the full content instead of a summary; persist it verbatim to the artifact path yourself
before applying the gate. Never let a gate fail over a host restriction the stage disclosed.

### Stage gates (check after each stage, before the next)

- **analyze** → `analysis.md` exists with all four sections (Found / Exemplars / Missing / Reuse plan).
- **plan** → `plan.md` contains a **Runnable check** section with an executable command. If missing,
  send the planner back once with that feedback; if still missing, stop and ask the user.
- **implement** → `implementation.md` exists with real command output under Evidence.
- **verify** → `test-report.md` contains a structured `PASS` or `FAIL` verdict with evidence.

### The verify isolation rule

The verifier judges the work fresh. It receives **only** the plan (or `task.md` for tier S) and the
profile — never the implementer's summary, reasoning, or artifact. An agent shown the rationale
tends to confirm it rather than break it.

The verifier must also not edit code. The verifier always holds a shell — it has to execute the
runnable check — and a shell can edit files, so no tool restriction makes editing impossible.
Enforcement is therefore two layers, and the first is never optional:

1. **Diff fingerprint** — mandatory on every run, in both modes. Record `git diff` (or its hash)
   immediately before verification and compare immediately after. If the working tree changed
   during verification, the verdict is void: discard the report and re-run verification. (The
   report itself lives in `.agent-loop/`, outside the tree, so writing it never trips this.)
2. **Tool restriction** — where your host supports it, additionally spawn the verifier without
   file-write tools. Defence in depth: it removes the convenient edit path and states intent, but
   the fingerprint is the guarantee.

## Step 3 — Debug loop (on FAIL)

Maximum **3 iterations**. For each:

1. Run **debug** (`references/stages/debug.md`) with paths to `plan.md` (or `task.md`),
   `test-report.md`, `implementation.md`, and the iteration number.
2. Run **verify** again, fresh, with the same restricted inputs as before. Never let the debugger
   certify its own fix.

Stop the loop early and escalate if:
- two consecutive debug iterations made substantively the same change (the loop is oscillating);
- the debugger reports the **plan itself** is wrong;
- 3 iterations are exhausted.

**Escalation:** write a distilled failure summary at the end of `test-report.md` — what was tried,
what still fails, current best hypothesis — and report to the user. Do not silently continue past
the cap, and do not re-plan without the user asking for it.

## Step 4 — Report

Tell the user: the tier and mode chosen, what changed (files, one line each), the final verdict,
where the evidence is, and the run-directory path. If `test-report.md` lists anything under **Not
verified**, surface it verbatim — a PASS covers only what was executed, and the remaining manual
checks are the user's to run. Keep it short — the artifacts are the audit log.
