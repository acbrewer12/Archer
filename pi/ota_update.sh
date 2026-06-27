#!/usr/bin/env bash
# ota_update.sh — Pull latest Archer code from GitHub and restart the service.
#
# Usage:
#   sudo bash /home/pi/Archer/pi/ota_update.sh
#
# What it does:
#   1. git fetch + reset to origin/main (force-clean to handle force-pushes)
#   2. pip install -r requirements.txt (only installs new/changed deps)
#   3. Restarts the archer.service systemd unit if it exists
#   4. Logs result to /var/log/archer_ota.log
#
# Set ARCHER_REPO if you forked the repo.
# Cron example (nightly at 3am):
#   0 3 * * * /bin/bash /home/pi/Archer/pi/ota_update.sh >> /var/log/archer_ota.log 2>&1

set -euo pipefail

REPO_DIR="${ARCHER_REPO_DIR:-/home/pi/Archer}"
LOG_FILE="/var/log/archer_ota.log"
SERVICE="archer.service"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "OTA update starting"
log "Repo: $REPO_DIR"

# ── 1. Pull latest code ──────────────────────────────────────────────────────
cd "$REPO_DIR"

BEFORE=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

git fetch origin main --quiet
git reset --hard origin/main --quiet

AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    log "Already up to date ($AFTER). Nothing to do."
    exit 0
fi

log "Updated: $BEFORE → $AFTER"
git log --oneline "$BEFORE..$AFTER" | while read line; do log "  + $line"; done

# ── 2. Install / update Python dependencies ──────────────────────────────────
log "Installing dependencies..."
pip install -r requirements.txt --quiet --break-system-packages 2>&1 | tail -5 | while read line; do log "  pip: $line"; done

# ── 3. Restart the systemd service if it exists ──────────────────────────────
if systemctl is-enabled "$SERVICE" &>/dev/null; then
    log "Restarting $SERVICE..."
    systemctl restart "$SERVICE"
    sleep 3
    STATUS=$(systemctl is-active "$SERVICE" || true)
    log "Service status: $STATUS"
    if [ "$STATUS" != "active" ]; then
        log "WARNING: Service did not come back up. Check: journalctl -u $SERVICE -n 50"
        exit 1
    fi
else
    log "No systemd service found ($SERVICE). Restart manually."
fi

log "OTA update complete."
