#!/usr/bin/env bash
# Runs on the team VM from the deploy checkout, after .github/workflows/deploy.yml checked out
# the commit to deploy (README, "Deploy from GitHub"). Backs up the running app, builds the
# image for this commit on the VM, starts it and waits until the app reports healthy.
# Reads .env beside docker-compose.prod.yml and never writes it.
#
#   bash deploy/vm-deploy.sh
#
# DEPLOY_HEALTH_TIMEOUT_SECONDS (300): how long the new container may take to become healthy.
# DEPLOY_KEEP_IMAGES (5): ca-mode images kept for rollback, including the one just deployed.
set -euo pipefail

cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.prod.yml)
health_timeout="${DEPLOY_HEALTH_TIMEOUT_SECONDS:-300}"
keep_images="${DEPLOY_KEEP_IMAGES:-5}"
# Overrides any CA_MODE_TAG in .env, so the image tag always names the checked-out commit.
CA_MODE_TAG="$(git rev-parse --short=12 HEAD)"
export CA_MODE_TAG

show_logs() {
  echo "--- last 200 lines of app logs ---"
  "${compose[@]}" logs --no-color --tail 200 app || true
}

if [[ ! -f .env ]]; then
  echo "::error::$PWD/.env is missing. Create it on the VM (README, \"Host it for a team\")."
  exit 1
fi
"${compose[@]}" config -q

if [[ -n "$("${compose[@]}" ps --status running --quiet app)" ]]; then
  echo "Backing up the running app"
  "${compose[@]}" exec -T app python -m scripts.backup now </dev/null
else
  echo "The app is not running (first deploy?), so there is nothing to back up"
fi

echo "Building ca-mode:$CA_MODE_TAG"
"${compose[@]}" build app

echo "Starting ca-mode:$CA_MODE_TAG"
if ! "${compose[@]}" up -d; then
  show_logs
  exit 1
fi

container="$("${compose[@]}" ps --all --quiet app)"
image="$(docker inspect --format '{{.Config.Image}}' "$container")"
if [[ "$image" != "ca-mode:$CA_MODE_TAG" ]]; then
  echo "::error::The app container runs $image, not ca-mode:$CA_MODE_TAG"
  exit 1
fi

deadline=$((SECONDS + health_timeout))
while :; do
  read -r status health restarts < <(docker inspect \
    --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} {{.RestartCount}}' \
    "$container")
  if [[ "$health" == healthy ]]; then
    break
  fi
  if [[ "$health" == unhealthy || "$status" == exited || "$status" == dead || "$restarts" -gt 0 ]]; then
    echo "::error::The app is $status (health: $health, restarts: $restarts)"
    show_logs
    exit 1
  fi
  if ((SECONDS >= deadline)); then
    echo "::error::The app was not healthy after ${health_timeout}s (health: $health)"
    show_logs
    exit 1
  fi
  sleep 5
done
echo "ca-mode:$CA_MODE_TAG is healthy"

tunnel_status="$(docker inspect --format '{{.State.Status}}' ca-mode-cloudflared 2>/dev/null || echo missing)"
if [[ "$tunnel_status" != running ]]; then
  echo "::error::cloudflared is $tunnel_status, so the app is not reachable"
  "${compose[@]}" logs --no-color --tail 50 cloudflared || true
  exit 1
fi

# Keep the newest images for rollback (README, "Deploy from GitHub"); an image a container still
# uses is refused by `docker image rm` and stays.
docker image ls ca-mode --format '{{.Tag}}' \
  | { grep -vx -e "$CA_MODE_TAG" -e '<none>' || true; } \
  | tail -n +"$keep_images" \
  | while read -r old; do
      docker image rm "ca-mode:$old" >/dev/null 2>&1 || echo "Kept ca-mode:$old (in use)"
    done
