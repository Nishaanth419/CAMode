# Skills Guide

This directory contains reusable, composable Claude Code skills from [ai-engineering-skills](https://github.com/Madheshvivekanandan/ai-engineering-skills) (commit `a6fe96a`) configured for this project. Skills 1–5 were originally taken from [AI_Skills](https://github.com/Nishaanth419/AI_Skills).

## Included Skills

### 1. **python-best-practices** ⭐ Most Critical
- **When to load**: Before writing, modifying, or reviewing any Python code
- **Why it matters**: The entire backend is Python (FastAPI, SQLite, MCP client, tests)
- **Covers**: Architecture layering, naming conventions, type safety, error handling, logging, FastAPI patterns, async code, testing, and tooling (ruff/black/mypy)
- **How to use**: Invoke `/python-best-practices` before touching `app/` or `tests/` files

### 2. **llm-application-best-practices** ⭐ Critical for This Project
- **When to load**: Before writing agent logic, prompts, tool schemas, or evaluation code
- **Why it matters**: CA Mode is fundamentally an LLM application with two specialized agents
- **Covers**: Prompt design, system prompts, tool/function-calling schemas, cost optimization, streaming, retries, guardrails, prompt-injection defense, and observability
- **How to use**: Invoke `/llm-application-best-practices` before modifying `app/agent.py` or adding new prompts

### 3. **api-contract-design-best-practices**
- **When to load**: Before adding or changing HTTP endpoints, request/response schemas, or error payloads
- **Why it matters**: FastAPI serves the REST API for the frontend and OAuth flow
- **Covers**: Resource modeling, HTTP semantics, RFC 9457 error formats, pagination, versioning, and OpenAPI documentation
- **How to use**: Invoke `/api-contract-design-best-practices` before modifying routes in `app/main.py`

### 4. **docker-deployment-best-practices**
- **When to load**: Before writing or reviewing `Dockerfile`, `docker-compose.yml`, or container deployment code
- **Why it matters**: Project has a Docker setup for local development and production deployment
- **Covers**: Multi-stage builds, base image pinning, secrets handling, non-root runtime, health probes, resource limits, and vulnerability scanning
- **How to use**: Invoke `/docker-deployment-best-practices` before touching Docker files or CI/CD deployment jobs

### 5. **git-commit-pr-workflow**
- **When to load**: Before committing code, pushing, or opening a pull request
- **Why it matters**: Ensures consistent and reviewable git history
- **Covers**: Atomic commits, Conventional Commits format, trunk-based branching, PR scope, CI/CD pipelines, semantic versioning, and review etiquette
- **How to use**: Invoke `/git-commit-pr-workflow` before running `git commit` or `gh pr create`

### 6. **agent-loop**
- **Source**: [Madheshvivekanandan/agent-loop](https://github.com/Madheshvivekanandan/agent-loop) v0.2.0 (commit `2c8341f`), with its five stage subagents in `.claude/agents/`
- **When to load**: Only when you explicitly want a task run through the staged loop — it is never auto-selected
- **Why it matters**: Multi-file features (e.g. a new agent or MCP tool flow) benefit from a separate plan, an independent verifier that runs `python -m pytest -q`, and a capped debug loop
- **Covers**: Triage (tier S/M/L), analyze → plan → implement → verify, up to 3 debug iterations, file-based handoff with run logs in `.agent-loop/` (git-ignored)
- **How to use**: `/agent-loop <task description>`

## Quick Reference

| File Change | Load This Skill |
|---|---|
| `app/**/*.py`, `tests/**/*.py` | `/python-best-practices` |
| `app/agent.py` (prompts, tools, logic), `app/snapshots.py` (agent snapshot tools, profile prompt text) | `/llm-application-best-practices` |
| `app/users.py` (PINs, sessions, tokens), `app/db.py` (schema, migrations) | `/python-best-practices` (Security and Database sections) |
| `app/main.py` (routes, schemas, errors) | `/api-contract-design-best-practices` |
| `Dockerfile`, `docker-compose.yml` | `/docker-deployment-best-practices` |
| Before commit/push/PR | `/git-commit-pr-workflow` |
| Larger task you want planned, verified, and debugged in stages | `/agent-loop <task>` |

## How to Invoke

In Claude Code, type:
```
/<skill-name>
```

Example:
```
/python-best-practices
```

The skill will load its guidance into your turn, helping you write code aligned with best practices for this project.

## Notes

- Skills are **additive guidance**, not strict rules—when in doubt, follow existing code style in the repo over a skill
- Load multiple skills in a single turn if working on related changes (e.g., Python + LLM + API in one session)
- Each skill includes a detailed reference section and a review checklist at the end
- See `.claude/CLAUDE.md` for project-specific architecture and commands
