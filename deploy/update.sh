#!/bin/sh
# Restart the hosted app on the newest image the "Publish image" workflow published.
# Run by deploy/ca-mode-update.timer every 5 minutes (docs/hosting.md: "Automatic updates"):
#   1. pull CA_MODE_IMAGE; stop if the app already runs it, or it failed its health check before
#   2. back up the database, remember the running image as ca-mode:previous
#   3. tag the new image ca-mode:current (what docker-compose.image.yml runs), restart the app and wait up
#      to 2 minutes for it to be healthy (/healthz: the process answers; the Mac may be asleep meanwhile)
#   4. if it is not, tag ca-mode:previous as ca-mode:current again, restart, and skip that image from then on
# The ca-mode:current tag is the state Compose and this script share: a later plain `docker compose up -d`
# runs whatever this script last deployed or went back to, never the image that failed.
# Everyone signed in enters their PIN again after a restart (unlocked keys are in memory only).
set -u

cd "${CA_MODE_DEPLOY_DIR:-/srv/ca-mode/app}" || exit 1
log() { echo "ca-mode-update: $*"; }

# A value from .env (Compose's file), so CA_MODE_IMAGE set there pins the version here too.
env_value() {
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 |
    sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

IMAGE="${CA_MODE_IMAGE:-$(env_value CA_MODE_IMAGE)}"
IMAGE="${IMAGE:-ghcr.io/nishaanth419/camode:main}"
STATE="${CA_MODE_UPDATE_STATE:-/srv/ca-mode/update}"
CURRENT=ca-mode:current
PREVIOUS=ca-mode:previous
mkdir -p "$STATE"

image_id() { docker image inspect --format '{{.Id}}' "$1" 2>/dev/null; }
app_container() { docker compose ps -q app 2>/dev/null; }
# Run $1 (an image name or id) as the app: tag it ca-mode:current, then recreate the app container.
run_image() { docker tag "$1" "$CURRENT" && docker compose up -d --no-build app; }

if [ "${CA_MODE_NO_PULL:-}" != 1 ]; then
  docker pull -q "$IMAGE" >/dev/null || { log "could not pull $IMAGE"; exit 1; }
fi
new=$(image_id "$IMAGE") || { log "no image $IMAGE"; exit 1; }
container=$(app_container)
running=$( [ -n "$container" ] && docker inspect --format '{{.Image}}' "$container" || echo none)
bad=$(cat "$STATE/bad" 2>/dev/null || true)

# The image that failed is running again (started by hand from an old tag): leave it again.
if [ -n "$bad" ] && [ "$running" = "$bad" ]; then
  previous=$(image_id "$PREVIOUS")
  if [ -n "$previous" ] && [ "$previous" != "$bad" ]; then
    log "the app runs $bad, which failed its health check before; going back to the previous image"
    run_image "$PREVIOUS"
  else
    log "the app runs $bad, which failed its health check before, and there is no other image to go back to"
  fi
  exit 1
fi

if [ "$new" = "$running" ]; then
  # Nothing to deploy. Keep ca-mode:current on the running image (the first run after this script changed).
  [ "$(image_id "$CURRENT")" = "$running" ] || docker tag "$running" "$CURRENT"
  exit 0
fi
if [ "$new" = "$bad" ]; then
  log "skipping $new: it failed its health check before"
  exit 0
fi

log "deploying $new (running: $running)"
if [ -n "$container" ]; then
  docker compose exec -T app python -m scripts.backup now || { log "backup failed, not deploying"; exit 1; }
  docker tag "$running" "$PREVIOUS"
fi
run_image "$IMAGE" || { log "could not start the new image"; exit 1; }

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
if [ -n "$(image_id "$PREVIOUS")" ]; then
  run_image "$PREVIOUS"
fi
exit 1
