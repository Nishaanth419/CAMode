#!/bin/sh
# Copy the hosted server's daily backups to this Mac over Tailscale, check the newest one,
# and keep the newest CA_MODE_BACKUP_KEEP copies. Run by launchd (README: "Backups"):
#   sh deploy/pull-backups.sh --install    # copy to ~/.local/bin and schedule twice a day
#   sh deploy/pull-backups.sh              # copy now
# Needs SSH to the server without a passphrase prompt and passwordless sudo there (the
# backups are root's, mode 600). Uses only macOS's own tools, so it runs outside the repo:
# launchd jobs cannot read ~/Desktop.
set -u

HOST="${CA_MODE_BACKUP_HOST:-ubuntu@100.94.11.69}"
SOURCE="${CA_MODE_BACKUP_SOURCE:-/srv/ca-mode/backups/}"
DEST="${CA_MODE_BACKUP_DEST:-$HOME/ca-mode-backups/server}"
KEEP="${CA_MODE_BACKUP_KEEP:-30}"
LABEL="com.camode.pull-backups"
INSTALLED="$HOME/.local/bin/ca-mode-pull-backups"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--install" ]; then
  mkdir -p "$(dirname "$INSTALLED")" "$(dirname "$PLIST")" "$DEST"
  cp "$0" "$INSTALLED" && chmod 755 "$INSTALLED"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/sh</string><string>$INSTALLED</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>CA_MODE_BACKUP_HOST</key><string>$HOST</string>
    <key>CA_MODE_BACKUP_DEST</key><string>$DEST</string>
    <key>CA_MODE_BACKUP_KEEP</key><string>$KEEP</string>
  </dict>
  <key>StartCalendarInterval</key><array>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$DEST/launchd.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "Scheduled $LABEL (09:30 and 21:30, and at login); copies go to $DEST"
  exit $?
fi

mkdir -p "$DEST" && chmod 700 "$DEST"
LOG="$DEST/pull.log"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
fail() {
  log "FAILED: $1"
  osascript -e "display notification \"$1\" with title \"CA Mode: server backup not copied\"" >/dev/null 2>&1
  exit 1
}

rsync -a --rsync-path="sudo rsync" -e "ssh -o BatchMode=yes -o ConnectTimeout=20" \
  "$HOST:$SOURCE" "$DEST/" 2>> "$LOG" || fail "could not copy from $HOST (is Tailscale on?)"
chmod 600 "$DEST"/codemode-*.sqlite3 2>/dev/null

NEWEST=$(ls -1t "$DEST"/codemode-*.sqlite3 2>/dev/null | head -1)
[ -n "$NEWEST" ] || fail "no backups on the server yet"
CHECK=$(sqlite3 "file:$NEWEST?mode=ro" "PRAGMA integrity_check;" 2>&1)
[ "$CHECK" = "ok" ] || fail "$(basename "$NEWEST") failed SQLite's integrity check: $CHECK"
PROFILES=$(sqlite3 "file:$NEWEST?mode=ro" "SELECT count(*) FROM users;" 2>&1) || fail "$(basename "$NEWEST") has no users table"

ls -1t "$DEST"/codemode-*.sqlite3 | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old"; done
log "ok: newest $(basename "$NEWEST") ($PROFILES profiles), $(ls -1 "$DEST"/codemode-*.sqlite3 | wc -l | tr -d ' ') copies kept"
