# Testing and evals

Moved out of the always-loaded CLAUDE.md. Read this when adding tests, changing fixtures, or before touching the agent, router, planner, tools or prompts. Folder rules: [../../tests/CLAUDE.md](../../tests/CLAUDE.md). Eval write-up and scores: [../evals.md](../evals.md).

## Strategy

Tests use mocked AI, MCP and OAuth responses (no live API calls). `conftest.py` points `app.db` at a temporary database per test, lowers the scrypt cost, disables the scheduler, and provides `client`, `signed_in` (a signed-in, unlocked profile that accepted the notice in `device` mode) and `sign_up()` (pass `storage_mode=None` to skip consent, or `"private"`).

`tests/` has one `test_<module>.py` per backend module (`ls tests/` is the coverage list), `tests/js/*.test.js` for the browser helpers (`node --test`), and shared fixtures in `tests/fixtures/`, `tests/mf_fixtures.py` and `tests/login_tool_brokers.py`. `app.main` re-exports `connect_db`, `initialize_db`, `get_session`, `save_session` and `touch_session` for tests.

## Evals: dataset, script, when to re-run

| Change | Run | Data |
|---|---|---|
| `ROUTER_PROMPT` (also bump `ROUTER_PROMPT_VERSION`) | `python -m scripts.eval_router --provider <p>` | `evals/request_routing.jsonl` |
| Portfolio prompt or `agent_payload()` | `python -m scripts.eval_investigations --provider ollama --runs 3` (`--regrade` re-scores an `--out` file) | `evals/portfolio_change.jsonl` |
| Agent loop, goal tools, task contracts | `python -m scripts.eval_tasks` (scripted); `--provider ollama --runs 3` (live cases) | `evals/agent_tasks.jsonl` |
| Planner, its prompts or budgets | `python -m scripts.eval_tasks --dataset evals/multi_step.jsonl`; live with `--provider ollama --runs 3` | `evals/multi_step.jsonl` |
| General agent, its tools or the guide | `python -m scripts.eval_workflows` (scripted); `--provider ollama --runs 3` live; `--baseline` runs the previous loop | `evals/financial_workflows.jsonl` |
| Local model choice | both of the above with `--provider ollama` | |
| Number checks | `python -m scripts.eval_numbers` | `tests/fixtures/indmoney_sample.py` |

Scripted modes are also run by `tests/test_eval_*.py`. A new eval case needs `expect` (and optionally `live.grade`). Live-case counts, baselines and scores are in the old CLAUDE.md text now kept under "Answer trace and Activity" / "Planned answers" / "General agent" in [agent-and-planner.md](agent-and-planner.md) and in [../evals.md](../evals.md).
