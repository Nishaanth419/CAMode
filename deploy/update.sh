#!/bin/sh
# Restart the hosted app on the newest image the "Publish image" workflow published.
# Run by deploy/ca-mode-update.timer every 5 minutes (README: "Automatic updates"):
#   1. pull CA_MODE_IMAGE; stop if the app already runs it
#   2. back up the database, remember the running image as ca-mode:previous
#   3. restart the app on the new image and wait up to 2 minutes for it to be healthy
#   4. if it is not, go back to ca-mode:previous and skip that image from then on
# Everyone signed in enters their PIN again after a restart (unlocked keys are in memory only).
set -u

cd "${CA_MODE_DEPLOY_DIR:-/srv/ca-mode/app}" || exit 1
IMAGE="${CA_MODE_IMAGE:-ghcr.io/nishaanth419/camode:main}"
STATE="${CA_MODE_UPDATE_STATE:-/srv/ca-mode/update}"
mkdir -p "$STATE"
log() { echo "ca-mode-update: $*"; }
app_container() { docker compose ps -q app 2>/dev/null; }

if [ "${CA_MODE_NO_PULL:-}" != 1 ]; then
  docker pull -q "$IMAGE" >/dev/null || { log "could not pull $IMAGE"; exit 1; }
fi
new=$(docker image inspect --format '{{.Id}}' "$IMAGE") || { log "no image $IMAGE"; exit 1; }
container=$(app_container)
running=$( [ -n "$container" ] && docker inspect --format '{{.Image}}' "$container" || echo none)
[ "$new" = "$running" ] && exit 0
if [ "$new" = "$(cat "$STATE/bad" 2>/dev/null)" ]; then
  log "skipping $new: it failed its health check before"
  exit 0
fi

log "deploying $new (running: $running)"
if [ -n "$container" ]; then
  docker compose exec -T app python -m scripts.backup now || { log "backup failed, not deploying"; exit 1; }
  docker tag "$running" ca-mode:previous
fi
CA_MODE_IMAGE="$IMAGE" docker compose up -d --no-build app || { log "could not start the new image"; exit 1; }

for _ in $(seq 1 24); do
  sleep 5
  health=$(docker inspect --format '{{.State.Health.Status}}' "$(app_container)" 2>/dev/null)
  if [ "$health" = healthy ]; then
    log "healthy on $new"
    rm -f "$STATE/bad"
    docker image prune -f >/dev/null
    exit 0
  fi
  [ "$health" = unhealthy ] && break
done

log "$new is not healthy; going back to the previous image"
echo "$new" > "$STATE/bad"
if docker image inspect ca-mode:previous >/dev/null 2>&1; then
  CA_MODE_IMAGE=ca-mode:previous docker compose up -d --no-build app
fi
exit 1
