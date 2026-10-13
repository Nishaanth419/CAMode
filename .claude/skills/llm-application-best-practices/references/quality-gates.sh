#!/usr/bin/env bash
# CI quality gates for an LLM application (see SKILL.md section 13).
# The pytest, git grep, git diff, and sha256sum shapes are standard; scripts/* names are
# patterns your team implements, not existing commands. Every path is a placeholder: point
# it at your repo's real prompt, tool, eval, and artifact directories, and keep the
# existence assertions. Run the adaptive red-team as a separate scheduled job, not here.
set -euo pipefail   # without it a failing pytest does not stop the script

pytest tests/evals -q --junitxml=reports/evals.xml   # every test asserts a documented threshold
pytest tests/evals/test_rag_metrics.py -q            # retriever and generator scored separately
pytest tests/evals/test_judge_position_bias.py -q    # both orderings; fail above a documented swap-disagreement ceiling
pytest tests/evals/test_citation_pointers.py -q      # every span resolves; quoted text matches byte-for-byte
pytest tests/evals/test_prompt_cache.py -q           # real pinned model, eval job: identical prefix twice => non-zero cache-read tokens
pytest tests/integration/test_idempotency.py -q      # retried side-effecting tool call reuses its key => exactly one side effect
pytest tests/integration/test_agent_limits.py -q     # terminates on step, depth, wall-clock, and cost ceilings
pytest tests/security/test_unicode_stripping.py -q   # SKILL.md section 8 invisible-Unicode ranges absent from prompt and render
pytest tests/security/test_retrieval_acl.py -q       # tenant-A query with forged tenant-B scope returns zero cross-tenant chunks
pytest tests/security/test_tool_permissions.py -q    # one out-of-scope operation per tool identity is denied
pytest tests/security/test_renderer_no_autofetch.py -q     # model-emitted images/link previews/iframes not auto-fetched
pytest tests/observability/test_no_content_capture.py -q   # OTel exporter: no prompt/completion content on spans by default

# judge model/prompt change forces an explicit re-baseline commit
# (needs origin/main fetched, not a single-commit shallow clone; judge config and baselines in separate tracked paths)
BASE=$(git merge-base origin/main HEAD)
if ! git diff --quiet "$BASE"..HEAD -- evals/judge.yaml; then
  if git diff --quiet "$BASE"..HEAD -- evals/baselines/; then
    echo 'judge config changed without a re-baseline'; exit 1
  fi
fi

sha256sum -c artifacts/CHECKSUMS                     # chat templates, tokenizer configs, adapters, quantization outputs
python scripts/lint_output_schemas.py schemas/       # reject unsupported schema keywords before they reach the API
python scripts/audit_tools.py --fail-on-open-ended   # no arbitrary shell/URL tool; every tool says when NOT to call it
python scripts/check_budgets.py reports/evals.json   # p95 latency and cost/request within the recorded route budget
python scripts/check_risk_mapping.py docs/design/    # design doc maps to OWASP LLM (and ASI where applicable) with an owner

# secrets must never appear in prompt or tool assets
# the existence assertions stop a stale pathspec plus `|| true` from passing having inspected zero files
git ls-files --error-unmatch prompts/ tools/ >/dev/null || { echo 'gate misconfigured: prompts/ or tools/ not tracked'; exit 1; }
git grep -nE '(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)' -- prompts/ tools/ && exit 1 || true
# nothing volatile in the cacheable prefix; deterministic serialization in prompt assembly
git ls-files --error-unmatch prompts/ src/prompts/ >/dev/null || { echo 'gate misconfigured: prompts/ or src/prompts/ not tracked'; exit 1; }
git grep -nE '(datetime\.now|Date\.now|time\.time|uuid4|uuid\.v4|random\.)' -- prompts/ src/prompts/ | grep -v test && exit 1 || true
git grep -n 'json.dumps' -- src/prompts/ | grep -v 'sort_keys=True' && exit 1 || true
