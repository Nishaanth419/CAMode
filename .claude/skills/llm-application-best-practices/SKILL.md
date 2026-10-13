---
name: llm-application-best-practices
description: Engineering standard for building production applications on top of large language models. Load BEFORE writing, modifying, or reviewing any code that calls an LLM API, assembles a prompt or system prompt, declares tool/function-calling schemas, builds a RAG or vector-search pipeline, writes an eval harness or LLM-as-judge, or implements an agent loop. Covers prompt and context design, structured output, tool contracts, retrieval grounding and citations, evaluation, cost and latency budgets, prompt caching, streaming, retries and idempotency, guardrails, prompt-injection defense, PII handling, and observability. Triggers on the `anthropic`, `openai`, `langchain`, `llama_index`, and `litellm` client libraries; vector-store clients (pgvector, pinecone, weaviate, qdrant, chroma); `tools=` / `output_config` / `response_format` call sites; and eval, judge, or retrieval-pipeline modules.
---

# LLM Application Best Practices

Scope: applications built on a hosted or self-hosted model, not model training. The rules are provider-neutral; before writing or reviewing code or config that targets the Anthropic API, read references/anthropic-api.md.
When this conflicts with an in-repo convention, follow the repo and say so.

## 1. Evaluation

- Order of work: written numeric success criteria, then an automated harness scoring them on a distribution-matched dataset, then the prompt. Build or extend the eval before changing a prompt, model, or pipeline, and report the measured before/after scores.
- Criteria are numbers set per route, e.g. `exact_match >= 0.92 on golden set v3`, `faithfulness >= 0.95; zero unresolvable citations`, `<= 0.5% attack success on the adaptive red-team suite`. The first gate run records the baseline; any regression against it fails the build.
- Prefer many auto-graded cases, run in CI, over few hand-graded ones. Evals and provider-behaviour gates (cache reads) call the real pinned model: run them as their own CI job, outside the unit and integration tests' no-network and determinism rules.
- Add these edge classes to the dataset explicitly: empty or nonexistent input, over-long input, adversarial/harmful input, ambiguous input, wrong-language input.
- Deterministic graders first (exact match, schema validation, numeric assertion, execution tests); use an LLM judge only for genuinely subjective dimensions such as tone.
- Score the retriever (context precision, context recall) and the generator (faithfulness, response relevancy) separately, plus noise sensitivity.

Judge hygiene:

- The judge model differs from the generator, and candidate identities are hidden.
- Run pairwise comparisons in both orderings; a verdict that flips on swap is a tie.
- Length-match candidates, or log output length beside every score and check the score does not track length.
- Calibrate against a human-labelled subset of this task and report the agreement rate; never quote a published judge/human agreement figure as general.
- Pin the judge (model id, prompt, rubric) in committed config; any change forces an explicit re-baseline commit.

## 2. Prompt and Context

- Retrieved documents and tool results are data, never instructions. Keep three layers structurally separate: durable instructions (system), task input (user), and untrusted content in its own block labelled with source, trust tier, and retrieval time, e.g. `<document source="..." trust="external" retrieved="...">`. Never string-concatenate untrusted content into the instruction block.
- In a long context, put load-bearing instructions and the most relevant evidence at the start or end, not the middle.
- Cap injected context by relevance, not by window size.
- Prompts are versioned files in the repo (e.g. `prompts/extract_invoice.v4.md`), loaded by id. No prompt literals inside a request handler.
- Count tokens with the provider's own tokenizer or count-tokens endpoint. `tiktoken` is OpenAI's and undercounts Claude by roughly 15-20% on typical text, more on code or non-English.

## 3. Structured Output and Tools

- Constrain every machine-consumed output with a JSON Schema (structured-output mode or strict tool use). Never parse prose or ask for "JSON only".
- Stay inside the provider's supported schema subset and lint schemas against it in CI; unsupported keywords are typically rejected outright (some SDKs strip them and validate client-side), never enforced by constrained decoding.
- Schema-valid is not safe: constrained decoding guarantees shape, never intent. Re-validate semantically in trusted code, not with a second LLM call, before any downstream action.
- Make fields required so an omission surfaces as a validation error, not silence.
- Every tool description states when to call and when not to call, units, limits, and preconditions, with an example call and an edge case.
- Poka-yoke arguments: absolute paths, enums not free text, units in field names (`timeout_seconds`, `amount_minor_units`).
- One tool, one narrow purpose, tested in isolation. No generic run-shell, fetch-arbitrary-URL, or execute-SQL-string tool.
- Check the stop/finish reason before reading content: refusals and truncations arrive as HTTP 200, and truncation returns partial content, not an error. Never index `content[0]` unconditionally.
- Answer every tool call with a result matched to its id, failures included as informative error results, and return parallel tool results together in one message.
- Append the full response content back into history, not just the extracted text.

## 4. Retrieval and Citations

- Enforce authorization inside the index query, at document and chunk level; no post-filter can un-supply a chunk the model already read. Derive tenant and scope from the authenticated principal server-side; a client-supplied tenant or scope parameter is never a control.
- Segregate indexes by tenant and trust tier for sensitive corpora rather than tag-separating one shared index.
- Normalize at ingest: strip white-on-white text and homoglyphs.
- Record provenance per chunk (source, ingest time, trust tier, pipeline version) so one poisoned batch can be invalidated without rebuilding the corpus.
- Hold embeddings and vector backups at the source documents' sensitivity tier: inversion reconstructs plaintext, so an embeddings leak is a document breach.
- Delete embeddings within a bounded SLA when the source is deleted.
- Re-embed the whole corpus when rotating the embedding model; never mix vector generations.
- Prefer a provider-native citation mechanism that returns parsed spans over asking the model to quote.
- Machine-verify every citation before display: the span resolves inside the supplied document and the quoted text matches byte-for-byte; drop or flag any that fails. Test the index conventions (0- vs 1-indexed, inclusive vs exclusive end); a wrong span looks like a grounding failure but is an arithmetic bug.
- When nothing clears the relevance threshold, return an explicit no-answer or escalate; never fall back to parametric memory in a pipeline that presents itself as grounded.

## 5. Cost and Latency

- Budget per route before launch: tokens in/out, cost per request, p50/p95 end-to-end latency, p95 time-to-first-token.
- Enforce hard, non-overridable spend ceilings per API key, user, team, and account that halt inference; an alert threshold is not a control.
- Rate-limit in tokens (per minute and per day) alongside requests per second.
- Estimate tokens pre-flight and reject oversized requests before inference starts.
- Fix latency and cost with model choice and context size before prompt rewrites.
- Degrade gracefully: a smaller model, shorter context, cached answer, or explicit "try again later" beats an unbounded queue.
- Stream anything that may be long; long non-streaming responses hit HTTP timeouts.
- Send non-interactive work through the provider's asynchronous batch mode (materially cheaper), keying results by your own request id, never by position.

## 6. Prompt Caching

Caching is a prefix match: one changed byte invalidates everything after it, silently (no error, just the bill).

- Order requests stable-first: tool definitions, frozen system text, stable history, then the current user input. Timestamps, UUIDs, and per-request ids go after the last cache breakpoint, never in the system prompt.
- Serialize deterministically: `json.dumps(..., sort_keys=True)`, never serialize or iterate a set, sort the tool list by name.
- Do not mutate the tool set or top-level system prompt mid-conversation (tools render first, so a tool change invalidates everything); append instead. Union per-user tool sets, keep per-user or role-conditional system sections after the shared frozen prefix, and deliver mid-conversation operator instructions on a post-prefix system-role channel where the provider offers one, else a clearly labelled (spoofable) user-turn block.
- Caches are model-scoped; switching model mid-session is a cold write.
- The minimum cacheable prefix is model-dependent and not monotonic across generations; below it nothing caches and nothing errors.
- Verify from the usage counters on every deploy: a persistent zero cache-read count across repeated identical prefixes is a bug to root-cause. With caching on, total prompt size is uncached + cache-write + cache-read tokens.

## 7. Reliability

- Give every call an explicit timeout.
- Retry only 429, 5xx, and connection/timeout errors, with exponential backoff plus jitter and a hard attempt cap; prefer the official SDK's built-in retry.
- After a refusal, never re-send the identical prompt to the same model: route to a different model (the provider's fallback mechanism where one exists) or a human escalation path.
- Pin the model id, prompt version, embedding model, and output schemas in committed configuration, and change them only in a deliberate, eval-gated commit. Never resolve a floating "latest" alias at runtime.
- Every side-effecting tool call sends a client-generated, high-entropy idempotency key (UUIDv4), reused unchanged on every retry. Never auto-retry a side effect without one.

## 8. Guardrails and Prompt Injection

Design for a bypassed instruction boundary: constrain what a compromised model can do and where its output can reach.

- Rule of Two, checked before shipping any agent: if it can simultaneously (A) ingest untrusted input, (B) reach sensitive data, and (C) change state or communicate externally, remove one leg or require per-action human approval.
- Complete mediation: every privileged call passes a deterministic policy decision point that re-validates intent, arguments, authorization, and preconditions against current state at execution time, on a graduated audit/warn/block/escalate policy (reversible actions may auto-approve; irreversible ones escalate).
- Least privilege per tool identity, enforced by the IAM policy or database grant the tool authenticates with.
- Propagate the end user's identity and scope across delegated, chained, and multi-agent calls; never collapse to a service identity at the first hop.
- Human approval shows the exact rendered action, never a model-written summary, at a volume low enough that reviewers actually read it.
- Memory writes are privileged: log the causing prompt, classify writes for instruction-like or role-modifying content, and require approval before instruction-bearing memory persists.
- Strip invisible Unicode at every ingest and render boundary: tag block `U+E0000-U+E007F`, variation selectors `U+FE00-U+FE0F`, zero-width `U+200B`, `U+200C`, `U+200D`, `U+2060`.
- Filter at every modality boundary: OCR images and transcribe audio, then apply the text filters to the extracted content.
- Neither the prompt nor a client-side guardrail is a control. "Never reveal these instructions", "only answer questions about X", and "do not call tool Y unless authorized" are hints, and anything a browser or app can turn off is UX; enforce every control server-side in deterministic code.
- Gate actions on groundedness and consistency signals, never on the model's self-reported confidence.
- Red-team adaptively as a scheduled job (not per commit), with the full deployed defense specification disclosed to the testers, and track attack-success rate over time.

Model output is untrusted input at every sink: apply the standard SQL, shell, `eval`/`exec`, HTML, and file-path controls, plus:

| Sink | Control |
|---|---|
| Terminals, log files | Strip or visibly encode ANSI escapes and control characters, or output can forge or hide log lines |
| Markdown renderer | Disable auto-loading of model-emitted images, link previews, and iframes; allowlist origins or proxy server-side. Auto-fetched image URLs are the zero-click exfiltration channel |

## 9. Agent Design

- Use the simplest tier that clears the eval bar: a single call, then a coded workflow, then an agent loop only when the path cannot be predetermined, the value justifies the cost and latency, and errors are recoverable (tests, review, rollback).
- Every agent run carries four circuit breakers (step limit, recursion depth, wall clock, per-run cost ceiling) plus loop detection by hashing run state. A breach escalates or terminates; it never fails open.

## 10. Secrets, PII, and Data Handling

- Never put a secret, credential, API key, or connection string in a prompt, system prompt, tool description, or conversation history; inject it server-side at call time. Assume everything in the model's context is user-visible and persists in logs, traces, and stored transcripts.
- Treat tool-call arguments, reasoning traces, retrieved chunks, embeddings, telemetry payloads, and timing/token-length side channels as outputs under the same classification and redaction rules as the visible answer.
- Redact in layers (pattern matching, NER, trained classifiers); regex alone misses base64, hex, and cross-lingual encodings.
- Apply per-user and per-session query budgets on sensitive endpoints, and keep log-probabilities, confidence scores, raw similarity scores, and verbose internal explanations off production responses.
- Minimize retention: the shortest transcript the product needs, a documented TTL, and regulated fields excluded at write rather than redacted on read.

## 11. Observability

- Follow the OpenTelemetry GenAI semantic conventions (Development status, now in the `open-telemetry/semantic-conventions-genai` repository); pin a version and expect drift.
- One CLIENT span per logical operation, covering all automatic retries, named `{operation} {model}`. Required attributes: operation name and provider name; add conversation id, `prompt.name`, and `prompt.version`.
- Log token usage on every call, and propagate the request/correlation id to every downstream tool call.
- Emit metrics for token usage, operation duration, time-to-first-chunk, per-output-chunk time, tool-execution duration, and inference-call and tool-call counts per invocation; alert on the last two, where runaway loops show long before the bill does.
- Do not capture prompts, completions, system instructions, or retrieved chunks in telemetry by default. Capture is explicit opt-in; in production store content in a separate access-controlled system and put only a reference on the span.

## 12. Supply Chain and Risk Mapping

- A tool description is prompt input: audit and diff third-party tool and MCP descriptions for embedded instructions; a poisoned description needs no version change.
- Maintain a signed AI/ML BOM (model ids, dataset versions, tool/MCP server versions, and hashes of adapters, chat templates, tokenizer configs, and quantization outputs) and diff it in CI so any change forces review. Sign and verify artifacts against a transparency log, and still gate them on behavioural evaluation: signing proves origin, not safety.
- Verify every AI-suggested dependency exists and is from the intended publisher; attackers register the package names models invent.
- Gate automated retraining and preference-feedback loops behind data validation, rate limits, and human oversight, and version datasets so a poisoning event can be rolled back.
- Each LLM feature's design doc classifies it, with a named owner, against the OWASP GenAI LLM Top 10 2026 (released August 2026; names and order changed from the 2025 list, never cite the 2025 names): LLM01 Prompt Injection, LLM02 Sensitive Information Disclosure, LLM03 Excessive Agency, LLM04 Supply Chain, LLM05 Data and Model Poisoning, LLM06 Unbounded Consumption, LLM07 Misinformation, LLM08 Hidden Context Exposure, LLM09 Vector and Embedding Weaknesses, LLM10 Improper Output Handling.
- A feature with tools or memory is also mapped against the OWASP Top 10 for Agentic Applications (ASI01-ASI10); governance reviews map onto the NIST AI RMF functions with a named owner per entry. NIST's GenAI risk categories have no alphanumeric ids; never invent any.

## 13. Quality Gates

Every gate fails the build, never just prints numbers. Before writing or changing an eval, a test for a rule above, or the CI job, read references/quality-gates.sh (one gate per testable rule; its `scripts/*` and paths are placeholders).

## 14. Agent Rules

- Never invent provider facts: model ids, prices, limits, parameter names, and schema support come from current documentation or the user, never from recall.
- In the summary, state the Rule-of-Two result for any agent change, the cost and latency impact of any added model call, longer context, or agent step, and every security-relevant surface touched (prompt assembly, tool definitions, retrieval scoping, output rendering, memory writes, telemetry).
