#!/bin/sh
# Every Cloudflare container starts on an empty disk: restore the database from R2 (a no-op on the very
# first start, when the bucket is empty), then run the app under Litestream, which streams each write to
# R2 and, on SIGTERM, stops the app and syncs once more before exiting.
set -eu

DB="${APP_DATA_DIR:-/data}/codemode.sqlite3"

litestream restore -config /etc/litestream.yml -if-db-not-exists -if-replica-exists -integrity-check quick "$DB"

# Exactly one worker: unlocked PIN keys, private chats, locks and the scheduler live in this process.
exec litestream replicate -config /etc/litestream.yml \
  -exec "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips *"
