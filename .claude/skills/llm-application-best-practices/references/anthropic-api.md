# Anthropic (Claude) API specifics

Read before writing or reviewing code or config that targets the Anthropic API. Everything here is provider- and version-specific; none of it holds for another vendor. Last reconciled against the claude-api skill on 2026-09-28. Where that skill is available it wins over this file, and the Models API (`GET /v1/models/{id}`: `max_input_tokens`, `max_tokens`, `capabilities`) is the live source for limits and feature support.

## Model ids

Exact strings, complete as-is; never append a date suffix.

| Model | Id | Context | Max output |
|---|---|---|---|
| Claude Fable 5.1 | `claude-fable-5-1` | 1M | 128K |
| Claude Fable 5 | `claude-fable-5` | 1M | 128K |
| Claude Opus 5.5 | `claude-opus-5-5` | 1M | 128K |
| Claude Opus 5 | `claude-opus-5` | 1M | 128K |
| Claude Opus 4.8 | `claude-opus-4-8` | 1M | 128K |
| Claude Opus 4.7 | `claude-opus-4-7` | 1M | 128K |
| Claude Opus 4.6 | `claude-opus-4-6` | 1M | 128K |
| Claude Sonnet 5 | `claude-sonnet-5` | 1M | 128K |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 1M | 128K |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | 64K |

Claude Mythos 5.1 (`claude-mythos-5-1`) and Claude Mythos 5 (`claude-mythos-5`) exist for Project Glasswing customers only.

## Thinking, effort, sampling

| Parameter | Current form |
|---|---|
| Thinking | `thinking: {type: "adaptive"}`. Omitting the field runs adaptive thinking on Fable 5/5.1, Opus 5.5, Opus 5, and Sonnet 5, but runs **without** thinking on Opus 4.8/4.7 (set adaptive explicitly there) |
| Fixed thinking budget | `thinking: {type: "enabled", budget_tokens: N}` returns 400 on Fable 5/5.1, Opus 5.5/5/4.8/4.7, and Sonnet 5, and is deprecated on Opus 4.6/Sonnet 4.6. Control depth with `effort` instead. Haiku 4.5 still requires it for thinking (minimum 1024, below `max_tokens`) |
| Disabling thinking | `{type: "disabled"}` returns 400 on Fable 5/5.1 and on Opus 5.5 at every effort: omit the field and lower effort instead. On Opus 5 it is accepted only at effort `high` or below (400 with `xhigh`/`max`). Accepted on Opus 4.8/4.7 and Sonnet 5 |
| Opus 5 with thinking disabled | The model occasionally writes a tool call into visible text instead of a `tool_use` block (the call never runs, no error) or leaks `<thinking>` tags. Prefer adaptive thinking at a lower effort |
| Depth | `output_config: {effort: ...}` with `low`, `medium`, `high`, `xhigh`, `max`; `effort` nests **inside** `output_config`, not top-level. Default `high`, except Opus 5.5 (default `medium`; set it explicitly). No `xhigh` before Opus 4.7; `effort` errors on Haiku 4.5 |
| `temperature`, `top_p`, `top_k` | Return 400 on Fable 5/5.1, Opus 5.5/5/4.8/4.7, and Sonnet 5; steer with prompting. Still allowed on Opus 4.6, Sonnet 4.6, and Haiku 4.5 |
| `max_tokens` | A hard cap covering thinking **plus** response text. A route that previously omitted `thinking` now thinks on Opus 5: re-check `max_tokens` on every such route before switching model ids. The SDKs require streaming for values near 128K |

## Structured output, tools, citations

- Structured output: `output_config: {format: {type: "json_schema", schema: {...}}}`. The older top-level `output_format` parameter is deprecated API-wide.
- Strict tool use: `strict: true` as a **top-level field on the tool definition** (alongside `name`/`description`/`input_schema`), not on `tool_choice`. The schema sets `additionalProperties: false` on every object and lists `required`.
- Supported schema subset: basic types, `enum`, `const`, `anyOf`, `allOf`, `$ref`/`$defs`, string formats `date-time`, `time`, `date`, `duration`, `email`, `hostname`, `uri`, `ipv4`, `ipv6`, `uuid`. **Not** supported: recursive schemas, numeric constraints (`minimum`, `maximum`, `multipleOf`), string length constraints (`minLength`, `maxLength`), complex array constraints, `additionalProperties` other than `false`. The Python and TypeScript SDKs strip unsupported constraints from the sent schema and validate them client-side.
- Structured outputs are incompatible with citations (400) and with message prefilling. Assistant-turn prefill returns 400 on Fable 5/5.1, Opus 5.5/5/4.8/4.7/4.6, Sonnet 5, and Sonnet 4.6; use structured outputs instead.
- `tool_choice`: `auto` (default) / `any` / `tool` / `none`. Forced `any` and `tool` return 400 on Fable 5.1, Mythos 5.1, and Opus 5.5 (also on `count_tokens` and Batches): use `auto` plus a prompt instruction naming the tool, with `strict: true` for schema-valid arguments, and check a call was made; or use structured outputs when the forced call only existed to get JSON. `disable_parallel_tool_use: true` caps a turn at one call.
- Parallel tool use is on by default: return **all** `tool_result` blocks, each with its matching `tool_use_id` and `is_error: true` for failures, in a single user message.
- Citations: set `citations: {enabled: true}` on **all or none** of a request's documents. Location types `char_location` (0-indexed, exclusive end), `page_location` (1-indexed, exclusive end), `content_block_location`; document indices are 0-indexed across the request; `cited_text` costs no output tokens.

## Prompt caching

| Aspect | Value |
|---|---|
| Marker | `cache_control: {type: "ephemeral"}`, optional `ttl: "1h"`; maximum **4** breakpoints per request. A top-level `cache_control` on the request auto-places a breakpoint on the last cacheable block and uses one slot |
| Render order | `tools` -> `system` -> `messages`. Caches are model-scoped, and changing tool definitions invalidates everything because tools render at position 0 |
| Economics | Reads about **0.1x** base input price (**0.025x** on Fable 5.1 and Mythos 5.1, **0.05x** on Opus 5.5); writes **1.25x** (5-minute TTL) and **2x** (1-hour TTL). 5-minute breaks even at two requests, 1-hour at three |
| Minimum prefix | **512** tokens on Fable 5/5.1, Opus 5.5, and Opus 5; **1024** on Opus 4.8, Sonnet 5, and Sonnet 4.6; **2048** on Opus 4.7; **4096** on Opus 4.6 and Haiku 4.5. Below it nothing caches and nothing errors (`cache_creation_input_tokens: 0`) |
| Mid-conversation operator instructions | Append `{"role": "system", "content": ...}` to `messages` instead of editing top-level `system`: it preserves the cached prefix and is the non-spoofable operator channel. Supported on Fable 5/5.1, Opus 5.5, Opus 5, Opus 4.8, and Mythos 5/5.1; **not** Sonnet 5 (400). No beta header. It must follow a `user` message (or an assistant message ending in server-tool use) and be either the last entry or followed by an `assistant` turn; never `messages[0]` |
| Verification | `usage.cache_read_input_tokens`. Total prompt size is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`; `input_tokens` is only the uncached remainder |

## Stop reasons, errors, retries, batch, tokens

- Handle `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`, and `model_context_window_exceeded`; check `stop_reason` before reading content.
- `pause_turn`: the server-side tool loop paused (default limit 10 iterations). Re-send the user message and the assistant response to resume; do not add a "Continue" message. The SDK tool runners do not auto-resume it.
- `refusal` arrives as HTTP **200**. Branch on `stop_reason` (or `stop_details.type`), never on `stop_details.category` or `stop_details.explanation`, which are `null` when a refusal maps to no named category; `stop_details` itself is `null` for every other stop reason. A refusal can arrive mid-stream after partial output: discard the partial output.
- Refusal recovery inside one call: the server-side `fallbacks` parameter (beta; Claude API and Claude Platform on AWS; rejected on Batches). Recommended form `fallbacks: "default"` with beta `server-side-fallback-2026-07-01` (routes by refusal category); that header also accepts the array form `fallbacks: [{"model": "claude-opus-4-8"}]`, while `server-side-fallback-2026-06-01` accepts only the array form; any other `server-side-fallback-*` value returns 400. On Bedrock, Vertex AI, and Foundry use the SDKs' client-side refusal-fallback middleware.
- Errors: 400 invalid_request, 401 authentication, 402 billing, 403 permission, 404 not_found, 413 request_too_large, 429 rate_limit, 500 api_error, 529 overloaded. Retryable: 429, 500-range, 529, connection errors; not retryable: the other 4xx.
- SDK defaults: `max_retries` 2, auto-retrying 408, 409, 429, 5xx, and connection errors with backoff; `timeout` 10 minutes, in seconds in Python/Ruby but milliseconds in TypeScript. Timeouts are retried, so wall clock can reach `timeout x (max_retries + 1)`.
- Message Batches: roughly **50%** cheaper and asynchronous; results arrive in any order, so key them by your `custom_id`, never by position.
- Tokens: count with `POST /v1/messages/count_tokens`, never `tiktoken`. The tokenizer introduced with Opus 4.7 (used by every later model, including Opus 4.8, Opus 5/5.5, Sonnet 5, and Fable 5/5.1) produces roughly 1-1.35x as many tokens as earlier models (about 30% on typical text): re-baseline counts when migrating from Opus 4.6, Sonnet 4.6, Haiku 4.5, or older.
