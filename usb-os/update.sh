#!/bin/bash
# update.sh — Pull latest Archer code and restart the app.
# Usage: sudo /opt/archer/usb-os/update.sh [--yes]
#
# SECURITY: this repo has no published release checksums/signatures or
# tagged-release convention yet (see usb-os/install.sh for the full
# rationale — checked via `git tag -l` / `git log`: only an unrelated
# `latest-apk` tag exists). REF below should be pinned to a specific
# commit or, once tags exist, `tags/vX.Y.Z` — not a mutable branch head
# — because this script does a blind `git reset --hard` to REF's remote
# tip on every run, on hardware with real OBD/GPIO/remote-start control
# over a vehicle. As a stopgap until signed/tagged releases exist, this
# script prints the exact commit it's about to reset to and requires
# either an interactive "y" confirmation or an explicit --yes flag
# before applying it.

ARCHER_DIR="/opt/archer"
REF="${ARCHER_REF:-claude/archer-truck-ai-system-TlfGE}"   # pin to a commit/tag, not a floating branch
VENV="$ARCHER_DIR/.venv/bin"

ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=true ;;
    esac
done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${BOLD}[UPDATE]${NC} $1"; }
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
die()  { echo -e "  ${RED}✗ $1${NC}"; exit 1; }

echo ""
echo -e "  ${BOLD}╔═══════════════════════════════════╗${NC}"
echo -e "  ${BOLD}║      ARCHER OS — UPDATE           ║${NC}"
echo -e "  ${BOLD}╚═══════════════════════════════════╝${NC}"
echo ""

# ── 1. Network check ─────────────────────────────────────────────────
log "Checking network..."
if ! curl -s --max-time 8 https://github.com -o /dev/null 2>&1; then
    die "Cannot reach GitHub — check internet connection and retry."
fi
ok "Network reachable"

# ── 2. Fetch latest code ─────────────────────────────────────────────
log "Fetching latest from GitHub..."
cd "$ARCHER_DIR" || die "Cannot find $ARCHER_DIR"

git fetch origin "$REF" --quiet 2>&1 || die "git fetch failed"

CURRENT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
LATEST=$(git rev-parse FETCH_HEAD 2>/dev/null) || die "Cannot resolve ref: $REF"

if [ "$CURRENT" = "$LATEST" ]; then
    ok "Already up to date ($(git rev-parse --short HEAD))"
    UPDATED=false
else
    warn "New commit available for ref '$REF':"
    echo "      $(git rev-parse --short "$CURRENT") → $(git rev-parse --short "$LATEST")  (full: $LATEST)"

    # ── Signature check — see pi/ota_update.sh's ONE-TIME SETUP comment.
    # Until commit signing is configured this always fails, which is why
    # --yes alone does NOT skip the human confirmation below: an unattended
    # run with no real verification and no human watching would otherwise
    # silently apply anything pushed to $REF, on hardware with real
    # OBD/GPIO/remote-start control.
    SIGNATURE_VERIFIED=false
    if command -v gpg &>/dev/null && git verify-commit "$LATEST" &>/dev/null; then
        SIGNATURE_VERIFIED=true
        ok "Signature check passed — $LATEST is GPG-signed by a trusted key."
    fi

    if [ "$SIGNATURE_VERIFIED" != "true" ]; then
        if [ "$ASSUME_YES" = "true" ]; then
            die "--yes was given but $LATEST is not a trusted GPG-signed commit, so this cannot proceed unattended. Configure commit signing (see pi/ota_update.sh) or re-run without --yes to confirm by hand."
        fi
        if [ -r /dev/tty ]; then
            printf "  Apply this update? This is a hard reset — local changes on the truck will be lost. [y/N] "
            read -r CONFIRM </dev/tty
        else
            CONFIRM="n"
        fi
        case "$CONFIRM" in
            y|Y) ;;
            *) die "Aborted by user." ;;
        esac
    fi
    git reset --hard "$LATEST" --quiet
    ok "Updated: $(git rev-parse --short "$CURRENT") → $(git rev-parse --short HEAD)"
    UPDATED=true
fi

# ── 3. Update Python packages if requirements changed ────────────────
log "Checking Python packages..."
"$VENV/pip" install -q -r "$ARCHER_DIR/requirements.txt" 2>&1
ok "Packages up to date"

# ── 4. Restart Archer ────────────────────────────────────────────────
# archer_init auto-restarts archer.py within 1 second of it dying.
if [ "$UPDATED" = "true" ]; then
    log "Restarting Archer app..."
    pkill -f "archer.py" 2>/dev/null || true
    ok "Archer restarting — refresh your browser in ~3 seconds"
else
    warn "No code changes — skipping restart"
fi

echo ""
echo -e "  ${GREEN}${BOLD}Update complete.${NC}"
echo ""
