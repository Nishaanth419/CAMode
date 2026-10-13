---
name: docker-deployment-best-practices
description: Production standard for containerizing and shipping an application. Load BEFORE writing or reviewing a Dockerfile, .dockerignore, docker-compose file, Kubernetes Deployment/Pod manifest, Helm values, or a CI job that builds, scans, tags, pushes, deploys, or rolls back a container image. Covers multi-stage builds, base image pinning, layer cache ordering, build-time and runtime secrets, non-root and hardened runtime, PID 1 and graceful shutdown, health probes, resource limits, image tagging/provenance/SBOM, vulnerability scanning gates, and rollback-safe deploys.
---

# Docker and Deployment Best Practices

Applies to any OCI image built with Docker/BuildKit and run under Docker, Compose, or Kubernetes. When this conflicts with an in-repo convention, follow the repo and say so.

## 1. Base Images

- Source: Docker Official Image, Verified Publisher, or Docker-Sponsored Open Source; otherwise your own trusted registry mirror.
- Variant: the smallest that runs the app. `-slim` (glibc) by default, distroless-style for compiled binaries, Alpine only after you have tested musl compatibility (DNS resolution and native-extension builds differ).
- Reference: `FROM <image>:<narrowest published variant tag>@sha256:<digest>`, never a bare major (`python:3`, `node:22`). Update digests deliberately via a bot PR.
- Rebuild and redeploy weekly, and immediately for a critical base-image CVE.

## 2. Dockerfile Authoring

- Order from least to most frequently changing: pinned `FROM`, system packages, non-root user creation, absolute `WORKDIR`, dependency manifests, dependency install, application source, then `USER` / `EXPOSE` / `HEALTHCHECK` / labels / `ENTRYPOINT`.
- One concern per image: no database, cron, or proxy beside the app.
- Multi-stage is mandatory for any compiled or dependency-installing app. The final stage holds runtime dependencies, the application package, and the files it reads at runtime outside the package (prompt files, templates, migration config), copied as an explicit list; `COPY . .` belongs only in a builder stage.
- The final stage creates its user and group explicitly, and its last identity change is `USER 10001:10001`: numeric, non-root, after every step that needs root.
- `apt-get update`, `apt-get install -y --no-install-recommends`, and `rm -rf /var/lib/apt/lists/*` in one `RUN`. Pin OS package versions (`libpq5=15.*`) and install app dependencies from the lockfile (`npm ci`).
- `COPY` for local files; `ADD` only for remote/Git sources, with `--checksum=sha256:...`.
- `chown` at copy time, numerically: `COPY --chown=10001:10001`. `RUN chown -R` duplicates the tree into a new layer; a named `--chown=app:app` fails the build if the user does not exist yet and does not match a numeric `runAsUser`.
- Piped `RUN`: `SHELL ["/bin/bash", "-o", "pipefail", "-c"]` once per stage. Plain `RUN set -o pipefail && ...` fails outright on Debian `-slim` images, whose `/bin/sh` is dash.
- Package-manager caches (pip, npm, apt, Go) via `RUN --mount=type=cache,target=<cache dir>`; build-only files via `RUN --mount=type=bind` so they never land in a layer.
- CI uses a registry cache (`--cache-from type=registry,ref=$REPO:buildcache --cache-to type=registry,ref=$REPO:buildcache,mode=max`); never deploy the cache ref. Registry cache export and attestations fail on the default `docker` driver unless it uses the containerd image store, so provision a builder as an explicit CI step (`docker buildx create --use --driver docker-container`).

```dockerfile
# syntax=docker/dockerfile:1
# check=error=true
FROM python:3.12-slim@sha256:<digest> AS builder
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    pip install --prefix=/install -r requirements.txt

FROM python:3.12-slim@sha256:<digest> AS runtime
RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=10001:10001 app/ ./app/
ENV PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"]
ARG GIT_SHA VERSION BUILD_DATE
LABEL org.opencontainers.image.source="https://github.com/org/repo" \
      org.opencontainers.image.revision="$GIT_SHA" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.base.name="python:3.12-slim" \
      org.opencontainers.image.base.digest="sha256:<digest>"
ENTRYPOINT ["python", "-m", "app.main"]
```

### .dockerignore

- Commit it at the build-context root, in the same change as the Dockerfile; a `.dockerignore` beside a Dockerfile outside the context root is silently ignored. Minimum entries: `.git`, `.env`, `.env.*`, `*.pem`, `*.key`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, `coverage`, `tests`, `test`, `docs`, `*.md`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `Dockerfile*`, `docker-compose*.yml`, `.github`, `**/*.log`, `!README.md`.
- Re-include anything packaging metadata reads at build time (`readme = "README.md"` in `pyproject.toml`, setuptools `long_description = file: README.md`), or the builder's dependency install fails with an opaque metadata error.

## 3. Secrets

| Phase | Do | Never |
|---|---|---|
| Build | `RUN --mount=type=secret,id=<id>` (file at `/run/secrets/<id>`, or `env=<VAR>` for that `RUN` only), passed as `--secret id=<id>,src=<path>` or `--secret id=<id>,env=<VAR>` | `ARG` / `--build-arg`, `ENV`, `COPY .npmrc` / `.netrc`, a token on a `RUN` line |
| Private Git/dependency fetch | `--ssh default` with an SSH mount, or the predefined `GIT_AUTH_TOKEN` / `GIT_AUTH_HEADER` secrets | a token in a repository URL |
| Runtime | orchestrator env vars or files mounted from a secret manager, read at startup; Compose `secrets:` with `file:` (`docker secret` exists only in Swarm mode) | image `ENV`; a value inside a committed manifest |
| Rotation | restart to reload, or re-read the mounted file on a signal | a rebuild to rotate a credential |

`ARG` is not safe for build secrets either: its values are recorded in `docker history` and `mode=max` provenance attestations.

## 4. Configuration and Logging

- All configuration comes from the environment or mounted files, never baked into the image.
- Processes are stateless: anything that must persist goes to a database, object store, or cache, never the container filesystem.
- Logs are structured lines on stdout/stderr with runtime and logger buffering disabled (`PYTHONUNBUFFERED=1`) so a killed container loses none; no in-container log files or logrotate.

## 5. PID 1 and Graceful Shutdown

- Exec form only: `ENTRYPOINT ["./app"]` for the binary, `CMD` for default overridable arguments; shell form runs under `/bin/sh -c`, which does not forward signals, so `docker stop` ends in SIGKILL.
- Entrypoint scripts end with `exec "$@"` so the app becomes PID 1.
- An app that spawns children needs an init that forwards signals and reaps (`docker run --init`; in Kubernetes an init or supervisor inside the image), or must spawn no unreaped children.
- On SIGTERM the app must: fail readiness immediately; stop accepting work (close the listener); finish in-flight requests within a deadline shorter than the grace period; for workers, return or NACK the in-flight job and release locks; flush logs and metrics; close DB pools and clients; exit 0.
- Every job is reentrant and idempotent: containers also die without SIGTERM, so at-least-once redelivery must be safe.
- Kubernetes removes endpoints asynchronously: add a `preStop` sleep of 5-15s matched to the proxy's propagation time. Use the native `Sleep` handler (verify your cluster version supports it); an `exec` sleep needs a shell and a `sleep` binary, which distroless-style images lack, so the hook fails (`FailedPreStopHook`) and the pod terminates without draining.
- `terminationGracePeriodSeconds` = preStop delay + worst-case drain + 5s (default 30s, then SIGKILL).

## 6. Health Checks and Probes

- Liveness is shallow, in-process, and never checks a downstream dependency. Readiness may check a dependency the instance cannot serve without, never a shared external API whose outage would pull every replica at once.
- Slow initialization gets a startup probe (liveness and readiness wait for it), not an inflated liveness `initialDelaySeconds` or `timeoutSeconds`.
- Set probe timings explicitly; the default `timeoutSeconds: 1` fails healthy-but-busy containers. Start from `timeoutSeconds: 3`, `periodSeconds: 10`, `failureThreshold: 3` for readiness and `failureThreshold: 6` for liveness.
- Use `httpGet` on dedicated routes: `/healthz` shallow, `/readyz` with dependencies.
- Health endpoints are unauthenticated and return status only: no versions, connection strings, or stack traces.
- Plain Docker/Compose: a `HEALTHCHECK` (or `--health-cmd`) calling the app's own health route with a short bounded command and no downloaded tooling, with interval, timeout, retries, and start period all explicit. `--start-period` defaults to `0s`, so a slow starter reports `unhealthy` through normal startup and blocks `depends_on: condition: service_healthy`.
- Docker takes no action on an unhealthy container (restart policies react to exit codes only): restart-on-unhealthy needs the app to exit non-zero itself or an orchestrator.

## 7. Resource Limits

- CPU and memory requests and limits on every container; a limit with no request implies an equal request.
- Size memory from measured peak RSS under representative load plus headroom. Be cautious with tight CPU limits: they throttle, producing latency spikes rather than errors.
- Untrusted or user-supplied workloads also cap processes and descriptors: `--pids-limit 256`, `--ulimit nproc=...`, `--ulimit nofile=...`.
- Bounded restarts: `--restart=on-failure:3` or a controller's backoff; a `CrashLoopBackOff` pages rather than looping silently.

## 8. Runtime Hardening

| Control | Docker | Kubernetes |
|---|---|---|
| Non-root | `--user 10001:10001` | `runAsNonRoot: true`, `runAsUser: 10001` |
| Read-only root filesystem | `--read-only --tmpfs /tmp` | `readOnlyRootFilesystem: true` + `emptyDir` volumes |
| Drop capabilities | `--cap-drop all --cap-add <needed>` | `capabilities: {drop: ["ALL"], add: [...]}` |
| No privilege escalation | `--security-opt=no-new-privileges` | `allowPrivilegeEscalation: false` |
| Never privileged | never `--privileged` | `privileged: false` |
| Syscall filtering | keep the default seccomp profile; add AppArmor/SELinux where supported | `seccompProfile: {type: RuntimeDefault}` |
| Never expose the daemon | never mount `/var/run/docker.sock` or expose the daemon over TCP; prefer rootless mode | no `hostPath` on the socket |
| Network exposure | publish to a specific interface (`-p 127.0.0.1:8000:8000`); user-defined networks, not the default bridge | Services + NetworkPolicy; no `hostNetwork` |
| Volumes | read-only where possible (`:ro`) | `readOnly: true` on volumeMounts |

- Docker-published ports bypass host firewall rules, so binding `0.0.0.0` on a public host silently exposes the service.
- `--read-only` needs a `tmpfs` for `/tmp` and every other path the runtime writes, or the container crashes at startup.

## 9. Tagging, Labels, and Provenance

- Tag every image `<repo>:<git-sha>`, plus `<repo>:<semver>` for releases.
- Deploy only a digest or an immutable tag: never `:latest` or a branch tag in a manifest, Compose file, or CI deploy step. An omitted `imagePullPolicy` is `Always` only for `:latest` or no tag and `IfNotPresent` otherwise, so a node that cached a mutable tag keeps running the old content.
- OCI labels (section 2 example) go in the last layer so per-build values do not bust the cache; CI passes `GIT_SHA`, `VERSION`, and `BUILD_DATE` as build args, never hardcoded in the Dockerfile.
- Build with `--sbom=true --provenance=mode=max` and push; the registry keeps the attestations. Verify with `docker buildx imagetools inspect <image> --format '{{ json .Provenance }}'`.
- Sign released images and verify signature plus policy at admission, pulling only from your own trusted registry. Pick one signer (Sigstore/cosign or Notation) and enforce it in the admission path; not Docker Content Trust, which Docker retires in December 2026.

## 10. CI Gates

Before writing or reviewing the image CI job, read references/ci-gates.sh (the full gate script). Every gate exits non-zero on failure; never `|| true`, a severity downgrade, or a blanket ignore file: fix the finding or escalate it with the scanner output. The gates check:

- Dockerfile: `docker build --check` and `hadolint`, with check warnings escalated on the build that ships (`# check=error=true` or `--build-arg BUILDKIT_DOCKERFILE_CHECK=error=true`).
- Build context: no secrets (`gitleaks dir .`; `gitleaks detect --no-git` is the deprecated form).
- One build, the one that ships: pushed as `$REPO:$GIT_SHA` (the push keeps the attestations), then pulled back for the local-store gates. A `pull_request` run gates a `--load` build instead, with no `--push`, `--cache-to`, or attestations: PR code never writes to the release registry or its cache.
- Vulnerabilities: fail on fixable HIGH/CRITICAL (`trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed`, `docker scout cves --exit-code --only-severity critical,high`); `docker scout quickview` has no `--exit-code` and never gates. Plus an image secret scan (`trivy image --scanners secret`).
- User: `Config.User` is non-empty and a non-zero numeric UID; hadolint cannot prove a `USER` was declared.
- Contents: no sensitive file in the image's file listing (`docker export` piped to `tar -tf`, which works on shell-less images; `docker history` cannot see file contents), and no credential in history `ARG`/`ENV` assignments (anchor the grep to them: a `RUN` using a secret mount legitimately shows `--mount=type=secret` in history).
- Hardened runtime: the image serves a real `/healthz` request under the section 8 flags plus pids, memory, and CPU limits; a `--version` check exits before the runtime opens its log, pid, or cache paths, so it passes with a missing `tmpfs`.
- Manifests: `kubesec scan` or `kubeaudit` for the section 7 and 8 controls; `conftest test k8s/` (OPA) to block images without a digest or missing probes/limits; `docker-bench-security` and `kube-bench` for CIS Benchmark conformance on hosts and clusters.
- Suppressions: review the unfixed set on a schedule; every suppression has an owner and an expiry date.

Run under `set -euo pipefail` and write every gate as `if <cmd>; then exit 1; fi` over output already materialised in a file or variable: `!`-negated commands are exempt from `set -e`, and a negated pipeline under `pipefail` turns a broken producer (unset `$REPO`, image not present locally) into a pass.

## 11. Deployment and Rollback

- Promote the digest that passed the gates in a lower environment; never rebuild per environment.
- Roll out through a controller with a surge-and-drain strategy, applying manifests from version control; never mutate a bare Pod or patch a running container.
- Set explicitly: `maxSurge: 25%` and `maxUnavailable: 0` for a stateless HTTP service; `minReadySeconds` 10-30s so a pod that crashes soon after becoming ready halts the rollout; `progressDeadlineSeconds`; a `revisionHistoryLimit` that allows rolling back more than one release.
- Rollback is one command documented in the runbook (`kubectl rollout undo deployment/<name> --to-revision=<n>`, confirmed with `kubectl rollout status deployment/<name> --timeout=5m`), and the previous artifact stays identifiable and pullable.
- Migrations and other admin tasks run as a separate one-off process (a Kubernetes Job) with the same image and config, never from the app's startup path. A release's migrations leave the schema usable by the previous release (expand/contract), or `rollout undo` is unusable; an app rollback leaves the schema in place.

## 12. Agent Rules

- Never invent a `sha256` digest; leave a clearly marked placeholder (`@sha256:<digest>`). Never invent versions, CVE IDs, image sizes, or tool flags.
- When touching a server or worker entrypoint, verify SIGTERM handling with `docker stop -t 30`: the container must log its shutdown line and exit 0 (143: no handler installed; 137: SIGKILL after the grace period, the signal never reached the process). If the app has none, say so rather than assuming the platform handles it.
- When a section 8 control blocks something, report the blocker rather than relaxing the control.
- Flag every security-relevant change in the summary: user, capabilities, mounted paths, published ports, network policy, and where each secret must come from.
