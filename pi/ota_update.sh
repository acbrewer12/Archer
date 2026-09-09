#!/usr/bin/env bash
# ota_update.sh — Pull latest Archer code from GitHub and restart the service.
#
# Usage:
#   sudo bash /home/pi/Archer/pi/ota_update.sh
#
# What it does:
#   1. git fetch, then verify origin/main is GPG-signed by a trusted key
#      (see ONE-TIME SETUP below) — abort if it isn't
#   2. git reset --hard to the now-verified origin/main (force-clean to
#      handle force-pushes)
#   3. pip install -r requirements.txt (only installs new/changed deps)
#   4. Restarts the archer.service systemd unit if it exists
#   5. Logs result to /var/log/archer_ota.log
#
# Set ARCHER_REPO if you forked the repo.
# Cron example (nightly at 3am):
#   0 3 * * * /bin/bash /home/pi/Archer/pi/ota_update.sh >> /var/log/archer_ota.log 2>&1
#
# ── ONE-TIME SETUP: commit signature verification ───────────────────────────
# This script refuses to apply an update unless the new commit is GPG-signed
# by a key you've explicitly trusted on the Pi. Without the setup below, that
# check fails CLOSED (every update gets rejected) rather than silently doing
# nothing — but it isn't actually *protecting* you until you've done this:
#
#   On your dev machine (NOT the Pi) — the box you push commits from:
#     1. gpg --full-generate-key                  # skip if you already have a key
#     2. git config commit.gpgsign true            # sign every commit from now on
#        git config user.signingkey <YOUR_KEY_ID>
#     (Existing unsigned history is fine — only the commit HEAD lands on
#      after `git reset --hard` needs to be signed.)
#
#   On the Pi — import and explicitly trust your PUBLIC key (once):
#     3. gpg --export --armor <YOUR_KEY_ID> > archer-release-key.asc
#        # copy archer-release-key.asc to the Pi yourself (scp/USB — do not
#        # fetch it over the same untrusted channel you're trying to protect)
#     4. gpg --import archer-release-key.asc
#     5. gpg --edit-key <YOUR_KEY_ID>
#          gpg> trust
#          Your decision? 5   (ultimate)
#          gpg> quit
#
# From then on, every commit you push to main must be signed (git commit -S,
# or rely on commit.gpgsign true) or this script will correctly refuse to
# deploy it. If you ever need to bypass this deliberately (e.g. recovering
# from a lost signing key), do it manually and audit the diff by hand first.

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

# ── Integrity check — do NOT skip this ──────────────────────────────────────
# This is the only thing standing between a compromised/MITM'd git remote and
# arbitrary code execution on hardware wired to the OBD/CAN bus and the
# remote-start relay. Fail CLOSED: any problem (unsigned commit, unknown/
# untrusted key, gpg missing, etc.) aborts the update and leaves the
# currently-running code untouched. See the "ONE-TIME SETUP" comment near
# the top of this file — until that's done, this check rejects everything.
if ! git verify-commit origin/main >>"$LOG_FILE" 2>&1; then
    log "REFUSING update: origin/main is not a trusted GPG-signed commit."
    log "See the ONE-TIME SETUP comment in ota_update.sh for how to configure signing/trust."
    exit 1
fi
log "Signature check passed — origin/main is GPG-signed by a trusted key."

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
