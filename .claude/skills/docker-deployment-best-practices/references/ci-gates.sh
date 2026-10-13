#!/usr/bin/env bash
# Image CI gates for docker-deployment-best-practices (see SKILL.md section 10).
# Every gate must fail the pipeline, not print a warning. Flags change between releases:
# confirm each against the tool's --help for the version you install. Expects REPO and GIT_SHA.

set -euo pipefail   # without this a failing gate below is only a message in the log

# Checks only, no image; exits non-zero if any violation is reported. Named rules include
# SecretsUsedInArgOrEnv, JSONArgsRecommended, WorkdirRelativePath, UndefinedVar, and
# CopyIgnoredFile; the set grows with BuildKit releases, so read the current build-checks reference.
docker build --check .
hadolint Dockerfile                         # lint: last USER not root (DL3002), pinned versions, no ADD
gitleaks dir .                              # no secrets in the build context (the deprecated form was `gitleaks detect --no-git`)

# Provision the builder explicitly: attestations and the registry cache need a non-default
# driver, or the containerd image store on the `docker` driver (SKILL.md sections 2 and 9).
docker buildx create --use --name ci --driver docker-container

# One build, and it is the one that ships. The build arg escalates check warnings to
# failures here too; a Dockerfile carrying `# check=error=true` needs no build arg at all.
if [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; then
  # PR code never writes to the release registry or its cache: load locally, no attestations.
  docker buildx build --build-arg "BUILDKIT_DOCKERFILE_CHECK=error=true" \
    --cache-from "type=registry,ref=$REPO:buildcache" --load -t "$REPO:$GIT_SHA" .
else
  docker buildx build --build-arg "BUILDKIT_DOCKERFILE_CHECK=error=true" \
    --cache-from "type=registry,ref=$REPO:buildcache" \
    --cache-to "type=registry,ref=$REPO:buildcache,mode=max" \
    --sbom=true --provenance=mode=max --push -t "$REPO:$GIT_SHA" .
  docker pull "$REPO:$GIT_SHA"              # --push keeps attestations; pull it back for the local-store gates below
fi

# --ignore-unfixed keeps the gate actionable; review the unfixed set on a schedule, and give
# every suppression an owner and an expiry date.
trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed "$REPO:$GIT_SHA"
trivy image --scanners secret --exit-code 1 "$REPO:$GIT_SHA"   # secret scanning is on by default for `trivy image`; this isolates it as its own failing gate
docker scout cves --exit-code --only-severity critical,high "$REPO:$GIT_SHA"   # gate: exit 2 on findings
docker scout quickview "$REPO:$GIT_SHA"     # informational only: quickview has no --exit-code, it never fails the build

test -n "$(docker image inspect --format '{{.Config.User}}' "$REPO:$GIT_SHA")"   # this, not hadolint, is what proves a USER was declared at all
docker image inspect --format '{{.Config.User}}' "$REPO:$GIT_SHA" \
  | grep -Eq '^[1-9][0-9]*(:[0-9]+)?$'      # ...and that it is a non-zero numeric UID (SKILL.md section 2)

# Contents, not history: history records instruction text and ARG values, never file contents,
# so only a file listing can prove a `COPY . .` did not sweep in `.env` or `.git`.
# Every gate is `if cmd; then exit 1; fi` over materialised output: a `!`-negated command is
# exempt from set -e, and under pipefail a negated pipeline turns a broken producer into a pass.
files=$(mktemp)
cid=$(docker create "$REPO:$GIT_SHA")
docker export "$cid" | tar -tf - > "$files"
docker rm "$cid"
if grep -Eiq '(^|/)(\.env($|\.)|\.git/|\.npmrc|\.netrc|\.aws/|id_(rsa|dsa|ecdsa|ed25519)$)|\.(pem|key|p12|pfx|jks)$' "$files"; then
  echo "sensitive file shipped inside the image" >&2; exit 1
fi
hist=$(mktemp)
docker history --no-trunc --format '{{.CreatedBy}}' "$REPO:$GIT_SHA" > "$hist"   # a broken producer aborts here under set -e
# Anchored to ARG/ENV assignments: a RUN consuming a secret mount legitimately shows
# --mount=type=secret and /run/secrets/<id> in history, so a full-history match would fail it.
if grep -Eiq '^(ARG|ENV) [^=]*(secret|token|password|api[_-]?key)[^=]*=' "$hist"; then
  echo "possible credential baked into ARG/ENV" >&2; exit 1
fi

# Hardened-runtime smoke test: the app must actually bind and serve under the flags.
# Invoking the image with --version would exit before the runtime opens any log, pid, or
# cache path, so a missing tmpfs mount would pass.
cid=$(docker run -d -p 127.0.0.1:18000:8000 --read-only --tmpfs /tmp --cap-drop all \
  --security-opt=no-new-privileges --pids-limit 256 -m 512m --cpus 0.5 \
  "$REPO:$GIT_SHA")
trap 'docker rm -f "$cid" >/dev/null 2>&1' EXIT
for _ in $(seq 30); do
  curl -fsS http://127.0.0.1:18000/healthz >/dev/null && break || sleep 1
done
curl -fsS http://127.0.0.1:18000/healthz >/dev/null   # served a request with a read-only root and only /tmp writable
[ "$(docker inspect -f '{{.State.Running}}' "$cid")" = true ]   # still up under the hardened flags
