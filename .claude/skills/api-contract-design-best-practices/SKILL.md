---
name: api-contract-design-best-practices
description: Framework-agnostic standard for designing HTTP/REST API contracts. Load BEFORE adding or changing any HTTP endpoint, request/response schema, error payload, OpenAPI/Swagger document, or outbound webhook — in any language or framework (FastAPI, Express, Spring, Rails, Go, .NET, gateway config). Covers resource modeling, method and status-code semantics, RFC 9457 problem+json errors, idempotency keys, cursor pagination, filtering and sorting, versioning and breaking-change rules, ETags and conditional requests, rate-limit signaling, contract-level authorization, webhooks, OpenAPI 3.1+, and CI contract gates.
---

# API Contract Design Best Practices

Target: HTTP JSON APIs specified in OpenAPI 3.1 or later.
When this conflicts with an in-repo convention, follow the repo and say so.

## 1. Resources and URLs

- Collections are plural nouns, items addressed by id: `/shipment-orders/{shipment_order_id}`.
- Path segments are `kebab-case` matching `^[a-z][a-z0-9-]*$` and case-sensitive: return `404` on a case mismatch, never normalize.
- No verb in any path segment (`POST /orders`, not `POST /orders/createNow`). The query string filters and pages; it never identifies or acts (`?action=cancel`).
- Nest only for genuine containment, at most 2 levels (`/orders/{order_id}/line-items`); every sub-resource is independently addressable (`GET /orders/{order_id}/line-items/{line_item_id}`).
- Model resources and fields on the caller's job, not your tables: `/order-header`, `/user-role-map`, or a response mirroring every column leaks the schema; ship only fields a caller needs.
- A non-CRUD action is a colon custom method on the resource, `POST /orders/{order_id}:cancel`, never a fake noun like `/orders/{order_id}/cancellation`. Use one only when get/list/create/update/delete cannot express the operation.
- Ids are opaque strings in the contract even when integers underneath, with one format per concept API-wide. Never expose a sequential id where enumeration is a business risk.

## 2. Methods

- `GET` and `HEAD` never mutate state: no view counter, no `last_seen_at` touch, no lazily created row.
- A repeated `DELETE` of an already-deleted resource returns `204` (or `200`), never `404`.
- `PUT` is full replacement: reject a partial body, never merge it.
- `PATCH` requires `application/merge-patch+json` (default) or `application/json-patch+json`; reject bare `application/json` with `415`. Use JSON Patch when explicit `null` is a meaningful stored value (merge patch reads `null` as delete and replaces arrays whole), for element-level array edits, or for `test` preconditions, and apply it atomically: any failing op means no mutation.
- Write bodies carry absolute values, never `{"increment": 1}`. Where a relative body is unavoidable, require `Idempotency-Key` in addition to `If-Match` and document the exception on that operation.

## 3. Status Codes

- `201` MUST carry `Location` and return the created representation.
- `401` MUST send `WWW-Authenticate`; `405` MUST send `Allow`; `503` sends `Retry-After` when known.
- `400` is malformed syntax or an invalid query parameter (unknown, unparseable, or a disallowed value), `422` is a well-formed body with invalid values (all body field validation); never collapse `409`, `412`, or `422` into `400`.
- Never `200` with an error body, and never a `{"success": false}` envelope.
- Long-running work returns `202` with the status-monitor URL in an `Operation-Location` header and a `monitor` body member, never in `Location` (clients read that as the created resource). The monitor carries `id`, a `status` from `NOT_STARTED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELED`, and on failure an `error` in the problem+json shape; retain it at least 24 hours.

## 4. Errors

Serve every `4xx`/`5xx` as `application/problem+json` per RFC 9457.

```http
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/validation-failed",
  "title": "Request body failed validation",
  "status": 422,
  "detail": "2 fields were rejected.",
  "instance": "/orders/9f2c1b",
  "code": "VALIDATION_FAILED",
  "request_id": "01J8Z3K9QW4",
  "errors": [
    { "pointer": "/items/0/quantity", "code": "OUT_OF_RANGE", "message": "must be between 1 and 500" },
    { "pointer": "/currency", "code": "UNSUPPORTED_VALUE", "message": "must be one of INR, USD, EUR" }
  ]
}
```

- `type` is a stable URI per problem class that clients branch on, preferably dereferenceable to its documentation; omit it (`about:blank`) only when the status code alone carries all the meaning.
- `title` is constant per `type`, varying only by localization; occurrence text goes in `detail`. `status` equals the status line.
- `instance` identifies the occurrence; the target resource URI is acceptable only when paired with a correlation-id extension such as `request_id`.
- Extension names are 3+ characters matching `^[A-Za-z][A-Za-z0-9_]*$`, and all machine-readable payload lives in extensions: always a stable `UPPER_SNAKE_CASE` `code`, plus an `errors` array of `{pointer, code, message}` for field validation.
- Never put stack traces, SQL, internal hostnames, upstream vendor error text, internal identifiers, or PII in an error body; log them and return the `request_id`.
- Keep a registry of `type` URIs and codes.
- One error envelope per API. If the organization already standardizes another (e.g. Azure's `error` object), use it everywhere; never mix two.

## 5. Idempotency Keys

Accept `Idempotency-Key` on every `POST` that creates, charges, ships, or notifies, and on the relative-body `PATCH` exception in §2; where a qualifying `POST` genuinely needs none, say why. Do not require or honor it on other methods.

| Aspect | Rule |
|---|---|
| Key | Client-generated, high-entropy (UUIDv4 or equivalent), at most 255 characters, no PII or secrets; reject a malformed key with `400` |
| Scope | Per endpoint and per authenticated principal or tenant, never global |
| Replay | Store the first request's status code, response body, and the headers its status requires (`Location` on a `201`); return them byte-for-byte for every later request with the key, including `4xx`/`5xx` produced once execution began |
| Fingerprint | Hash the request body with the key. Same key with a different body is `409` with problem `type` `.../idempotency-key-reused` (`422` only if the organization already uses it), never a replayed `2xx` |
| Not stored | Input-validation failures, and collisions with a concurrently executing request on the same key; return a documented retryable error for both |
| Concurrency | Lock the key or use a unique constraint so simultaneous retries cannot both execute |
| Retention | Documented; 24 hours is the baseline. After pruning, a reused key executes fresh |

Azure-aligned estates use `Repeatability-Request-ID` / `Repeatability-First-Sent` instead; pick one scheme per organization.

## 6. Pagination

- Paginate every collection endpoint from its first release, however small the table is today.
- Default to cursor pagination. Use offset only for small, static, human-browsed sets that need "jump to page N", and document that results may shift.
- Request `page_size` and `page_token`; respond `{"items": [...], "next": ...}`, where `next` is the following `page_token` or an absolute URL carrying it. Same names and one `next` form API-wide; `limit`/`cursor` is an acceptable house alternative, one per organization, linted.
- Default page size is documented and finite (20 to 50), never "all". Enforce a hard maximum: coerce oversize requests down to it; reject negative or non-numeric values with `400`.
- The token is opaque and URL-safe and never carries or widens authorization. A base64 offset or a JSON blob naming a table or sort key is not opaque: sign it, encrypt it, or store it server-side.
- End of collection is signaled only by an absent `next` (omit it, never `null`); a page may be short or empty before the end.
- Total count is optional and documented as an estimate.
- Document token expiry (a few days is typical); an expired token is `400` with a specific problem `type`.
- The cursor needs a total order: pair the sort key with a unique tiebreaker (`created_at`, then `id`).

## 7. Filtering, Sorting, and Expansion

- Allowlist filterable and sortable fields with documented operators.
- Reject unknown or unsupported query parameters with `400` problem+json; never silently ignore them (`?statu=open` would return the whole collection).
- Filters: one parameter per field for equality (`?status=OPEN`) and a closed set of suffixed operators for ranges (`?created_at_gte=2026-01-01T00:00:00Z`, `?amount_lt=500`). Adopt an expression language (OData `$filter`, RSQL) only where the organization already standardizes one.
- Repeated parameters OR within a field, separate parameters AND across fields: `?status=OPEN&status=HELD&currency=INR` is `(OPEN or HELD) and INR`. Use this or comma-separated values, never both, and state it in the spec.
- One sort syntax everywhere, `?sort=created_at,-id` (leading `-` is descending), and a documented default sort.
- Cap `?expand=` depth and breadth; no expansion may fan out unboundedly.

## 8. Representation

| Concern | Rule |
|---|---|
| Casing | One property casing API-wide (`snake_case` or `camelCase`), stated and linted; query and path parameter names use the same casing (examples here use `snake_case`) |
| Enums | `UPPER_SNAKE_CASE` strings, never integers; document the closed set |
| Absent values | Omit the member; explicit `null` only when it is a distinct, documented state |
| Timestamps | RFC 3339 UTC with an explicit offset (`2026-01-31T09:15:00Z`), named `*_at` |
| Durations | Unit in the name: `timeout_seconds`, `ttl_ms` |
| Money | Integer minor units plus an ISO 4217 currency code, or a decimal string; never a float |
| Booleans | `is_*` / `has_*`; never tri-state via `null` |
| Links | Absolute URLs in named members (`monitor`, `self`) where they save the client a lookup; never a HATEOAS link graph clients must traverse |
| Link relations | Registered relations (`next`, `deprecation`) are bare tokens; a relation you invent is a lowercase absolute URI you control |
| Response bodies | Always an object, never a bare scalar or array |

## 9. Authorization and Limits

- TLS only; credentials, tokens, and signatures never go in the query string or path.
- Authorize the specific object, not just the route: scope every query by tenant and owner derived from the authenticated principal, never from a request parameter; IDOR is the default bug.
- Return the same response for "absent" and "not yours": `404`, not `403`, where confirming existence leaks. Document it so clients do not read `404` as "safe to create".
- Authorize properties: bind request bodies to explicit per-operation, per-role request schemas, never to the persistence model or the response schema, so no caller can set `role`, `owner_id`, or `balance`. Mark server-owned fields `readOnly` and secrets `writeOnly`.
- Document every limit as part of the contract: rate limits per endpoint, principal, and tenant; maximum body size and array lengths.

## 10. Versioning and Compatibility

- Default to not versioning: extend compatibly. When you must, use one mechanism API-wide, by default a single major path segment (`/v1/...`) bumped almost never. Never version per endpoint (`/v1/orders` beside `/v3/orders`) and never mix mechanisms.
- Media-type versioning via `Accept` and a required dated `api-version=YYYY-MM-DD` parameter are legitimate alternatives: follow whichever the organization already standardizes, and prefer the dated parameter for frequent dated behavior changes shipped to a large external client base that must stay pinned.
- Never infer a version. Under header or query versioning, a missing or unknown version is `400` with a specific problem `type`. Under path versioning an unversioned URL is `404`; add no catch-all route.

Breaking, and forbidden within a major version:

- Removing or renaming a field, endpoint, or enum member
- Adding a required request field
- Narrowing a type (`string` to `integer`, wider to narrower numeric)
- Tightening validation (new `maxLength`, stricter regex, new required combination)
- Making an always-populated response field sometimes absent or `null`
- Changing a default value, or whether defaults are serialized
- Changing an id or resource-name format, looser or stricter
- Changing the `type`, `code`, or status for the same error condition
- Changing observable behavior or semantics for an unchanged request, including repurposing a field
- Adding pagination to an existing collection

Non-breaking only once the tolerant-reader contract is published: an optional request field with a safe default; a new response field; a new endpoint, method, or optional query parameter; a new error `type` for a genuinely new condition under an existing status. A new member of a response enum is borderline: announce it.

Publish the tolerant-reader contract explicitly: clients MUST ignore unknown response fields, enum members, and problem+json extension members, and MUST NOT depend on property order or on a field's absence.

Deprecation signals:

```http
Deprecation: @1688169599
Sunset: Sun, 31 Dec 2028 23:59:59 GMT
Link: <https://api.example.com/deprecation-policy>; rel="deprecation"
```

- `Deprecation` (RFC 9745) is a Structured Field date (`@` plus Unix seconds), past or future, and changes no behavior. `Sunset` (RFC 8594) is an HTTP-date no earlier than the `Deprecation` date.
- Instrument a deprecated operation per caller before setting its sunset; after sunset return `410 Gone`, not `404`.

## 11. Conditional Requests

- Every single-resource `GET` returns an `ETag` and honors `If-None-Match`: a match is `304`, repeating the `ETag` and `Cache-Control` the `200` would carry. ETags are strong by default; use weak (`W/`) only for deliberate semantic equivalence.
- Require `If-Match` on every `PATCH`, and on `PUT` and `DELETE` for any resource with concurrent writers; a mismatch is `412`. A required-but-missing `If-Match` is `428` by default (`412` if clients already special-case it; document which), never `200`.
- Evaluate preconditions in order: `If-Match`, `If-Unmodified-Since`, `If-None-Match`, `If-Modified-Since`.
- Derive the ETag deterministically from stored state (a version column, or a hash of a canonically ordered serialization), never from a request-time timestamp, random id, object address, or hash over an unordered map.
- Set `Cache-Control` deliberately; anything user-scoped is `private`.

## 12. Rate Limits

- A throttled request gets `429` with `Retry-After`, always. `Retry-After` never points earlier than the end of the effective window, and takes precedence over `RateLimit` when both are present.
- Advertise quota with the IETF `RateLimit-Policy` and `RateLimit` fields:

```http
RateLimit-Policy: "burst";q=100;w=60,"daily";q=1000;w=86400
RateLimit: "burst";r=50;t=30
```

- `RateLimit-Policy` items carry `q` (quota, required) plus optional `qu` (quota unit), `w` (window seconds), `pk` (partition key); `RateLimit` items carry `r` (remaining, required) plus optional `t` (effective window in seconds, not a guaranteed reset), `pk`.
- These fields come from an Internet-Draft (`draft-ietf-httpapi-ratelimit-headers`), not an RFC: call them "draft" in the docs and re-check names and parameters against the current draft.
- `X-RateLimit-Limit` / `-Remaining` / `-Reset` may stay for compatibility, documented as legacy aliases, never as a standard.

## 13. Webhooks (outbound events)

| Concern | Rule |
|---|---|
| Transport | HTTPS only, TLS 1.2+; reject plaintext receiver URLs at registration |
| Signature | HMAC-SHA256 over `timestamp + "." + raw_body`, in a dedicated header carrying the timestamp and a scheme identifier |
| Verification | Constant-time comparison over the unparsed raw bytes; ignore unknown or superseded schemes so a downgrade cannot pick a weaker one |
| Replay window | Reject timestamps outside a documented tolerance: 5 minutes by default, never 0 |
| Secret rotation | Overlap window with both secrets valid and one signature per active secret; document it (24 hours by default) |
| Delivery | At-least-once and unordered, stated in the docs: a `deleted` event can arrive before the `created` |
| Consumer contract | Verify the signature, durably persist or enqueue the raw event, then return `2xx`, all before any business logic. Deduplicate on event `id` (fallback: object id plus event type) |
| Retries | Exponential backoff over a documented window (hours to a few days); a `3xx` counts as a failure |
| Envelope | Stable `id`, `type`, `created`, an explicit payload version, and a reference to the affected resource. An emitted event stays pinned to the version in effect at creation and is never rewritten |
| Subscription | Subscribers select event types; never force a firehose |
| Receiver routes | Exempt from CSRF token middleware. IP allowlisting is a second control, never a substitute for signature verification |

## 14. OpenAPI

- Design first: write the OpenAPI change before the handler and ship both in the same change; a public-surface change without a spec change is incomplete. In a code-first framework (FastAPI), stub the schemas and route, commit the exported spec, and review that diff before the implementation; CI fails when the committed export is stale.
- One self-contained document per API with `info.title`, `info.version`, `contact`, and an owning-team and audience marker. Inventory every endpoint and every live version, old majors included.
- From 3.1 (3.2.0 is current) the Schema Object is JSON Schema 2020-12: write `type: ["string", "null"]` (`nullable` was removed, not deprecated), use the `examples` array instead of schema-level `example`, and set `jsonSchemaDialect` if you deviate from the default.
- Describe outbound webhooks in the top-level `webhooks` field with the same schema rigor as operations.
- Reuse through `components` (schemas, parameters, responses, `securitySchemes`); never duplicate a schema inline.
- Every operation declares an `operationId`, at least one tag, a `security` requirement (explicit `security: []` when deliberately public), its safe/idempotent/cacheable properties, every response code it can emit, and every required response header.

## 15. Gates

```bash
spectral lint openapi.yaml --ruleset .spectral.yaml --fail-severity=warn
# --fail-on ERR is required: without it oasdiff reports breaking changes and exits 0
oasdiff breaking --fail-on ERR released/openapi.yaml openapi.yaml
# informational: changelog exits non-zero only when a spec fails to load, which must not read as a breaking change
oasdiff changelog released/openapi.yaml openapi.yaml || true
```

- Before writing or reviewing the CI contract job, `.spectral.yaml`, or the live contract tests, read references/contract-gates.md.
- Confirm flag names against the installed tool versions.

## 16. Agent Rules

1. Classify every contract change as breaking or non-breaking against §10 and say which. If it is breaking, stop and propose the compatible alternative or the version-and-deprecation path; never ship it silently.
2. Never invent business semantics: if status transitions, currency rounding, retry windows, quota tiers, or which fields are required are ambiguous, ask.
3. Flag security-relevant surfaces you touch in the summary: auth, tenancy scoping, id handling, outbound URLs, webhook verification, quotas.
