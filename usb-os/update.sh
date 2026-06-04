#!/bin/bash
# update.sh — Pull latest Archer code and restart the app.
# Usage: sudo /opt/archer/usb-os/update.sh

ARCHER_DIR="/opt/archer"
BRANCH="claude/archer-truck-ai-system-TlfGE"
VENV="$ARCHER_DIR/.venv/bin"

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

git fetch origin "$BRANCH" --quiet 2>&1 || die "git fetch failed"

CURRENT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
LATEST=$(git rev-parse "origin/$BRANCH" 2>/dev/null) || die "Cannot resolve branch"

if [ "$CURRENT" = "$LATEST" ]; then
    ok "Already up to date ($(git rev-parse --short HEAD))"
    UPDATED=false
else
    git reset --hard "origin/$BRANCH" --quiet
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
