# API Contract Gates

Read when writing or reviewing an API's CI contract job, its project `.spectral.yaml`, or its live contract tests. The rules these gates enforce are defined in `../SKILL.md`; the `spectral lint` and `oasdiff` commands are in its Gates section.

## Spec greps

Each line is a gate: a non-zero exit fails the build. The greps are negated because a match is the violation.

```bash
# 'test -f' first: '! grep' turns grep's exit 2 (missing or unreadable file) into a passing gate
test -f openapi.yaml || exit 1
# '|| exit 1' is required: 'set -e' (the GitHub Actions bash default) ignores the failure of a '!'-negated
# command, so a bare '! grep' that matches fails the build only when it is the script's last line
! grep -n 'nullable:' openapi.yaml || exit 1   # removed in OpenAPI 3.1: use type: ["x", "null"]
! grep -nE '(^|/)(get|create|update|delete|list|fetch|remove)([A-Z][A-Za-z0-9]*)?([/:"]|$)' openapi.yaml || exit 1
# ^ verbs as a whole path segment (/orders/create, /users/delete) or camelCase-prefixed
#   (/getShipmentOrder, /orders/createNow). Anchoring on the segment boundary leaves genuine
#   hyphenated nouns (/delete-requests, /update-schedules) alone, so no allowlist is needed;
#   only a legitimate segment like /create-only-mode would - pipe it through `grep -v`.
```

## Project Spectral rules

Write each as a custom rule in `.spectral.yaml`; each fails the build.

```text
problem-json-on-errors      # every 4xx/5xx declares content['application/problem+json']
www-authenticate-on-401     # every declared 401 declares a WWW-Authenticate header
security-on-every-operation # explicit `security: []` for public operations
pagination-house-names      # collection operations declare page_size/page_token
location-on-201             # every creating POST declares a Location header on its 201
operation-id-and-tag        # every operation has an operationId and at least one tag
```

## Live contract probes

Run against a running instance.

| Probe | Expected |
|---|---|
| Same `POST` body twice with one `Idempotency-Key` | Byte-identical status and body |
| Same key, different body | `409` (or the documented `422`) problem+json, never a replayed `201` |
| `DELETE` twice | Same `2xx` both times, never `404` on the second |
| `GET`, then re-`GET` with `If-None-Match` | `304` |
| `PUT` with a stale `If-Match` | `412` |
| `PATCH` with no `If-Match` (any resource), or `PUT`/`DELETE` with no `If-Match` on a contested resource | `428` (or the documented `412`), never `200` |
| `PATCH` with `Content-Type: application/json` | `415` |
| Wrong method on a resource | `405` with `Allow` |
| Unauthenticated request to a protected operation | `401` with `WWW-Authenticate` |
| Request another tenant's object id | The documented code, identical to "absent" |
| Request maximum page size + 1 | Coerced down to the documented cap |
| Follow page tokens to exhaustion | Terminates on an absent token, not on a short page |
| Unknown query parameter | `400`, not a silently unfiltered collection |
| Exceed the rate limit | `429` with `Retry-After` and the rate-limit fields |
