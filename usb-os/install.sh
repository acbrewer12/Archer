#!/bin/bash
# ═══════════════════════════════════════════════════════
#  ARCHER USB OS — One-shot installer
#
#  SECURITY: Do NOT curl-pipe this straight into `sudo bash`. This repo
#  has no published release checksums or signatures yet (see the
#  TODO(release process) note below), so piping an unverified script
#  fetched over HTTPS straight into a root shell hands arbitrary code
#  execution to anyone who can push to $REPO/$REF — on hardware with
#  real OBD/GPIO/remote-start control over a vehicle. Until signed
#  releases exist, download first, verify, then run:
#
#    curl -fsSL -o /tmp/archer-install.sh \
#      https://raw.githubusercontent.com/acbrewer12/Archer/<PINNED_TAG_OR_SHA>/usb-os/install.sh
#    curl -fsSL -o /tmp/archer-install.sh.sha256 \
#      https://raw.githubusercontent.com/acbrewer12/Archer/<PINNED_TAG_OR_SHA>/usb-os/install.sh.sha256
#    sha256sum -c /tmp/archer-install.sh.sha256   # must say "OK" before continuing
#    sudo bash /tmp/archer-install.sh --yes
#
#  TODO(release process): this project currently has no tagged-release
#  convention (checked `git tag -l` / `git log`: only an unrelated
#  `latest-apk` tag exists, nothing like a `usb-os-vX.Y.Z`). Once one
#  exists, publish install.sh.sha256 (ideally with a detached GPG
#  signature, install.sh.sha256.asc, signed by a known Archer release
#  key) alongside every tagged release so the checksum step above has
#  something real to verify against. Until that infrastructure exists,
#  this comment documents the gap rather than papering over it.
# ═══════════════════════════════════════════════════════
set -e

ARCHER_DIR="/opt/archer"
ARCHER_PORT=5000
ARCHER_USER="archer"
REPO="https://github.com/acbrewer12/Archer.git"

# ── Pin to a specific ref, not a floating branch ─────────
# SECURITY: REF must point at a specific commit SHA or (once a tagging
# convention exists) a tag like "tags/vX.Y.Z" — never a mutable branch
# name such as "main" or a long-lived feature branch. Anyone who can
# push to a branch this points at controls what runs as root on this
# truck's OBD/GPIO/remote-start hardware on every install. Re-pin this
# by hand when you want to move to newer code; don't let it silently
# track a branch HEAD.
REF="${ARCHER_REF:-claude/archer-truck-ai-system-TlfGE}"

ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=true ;;
    esac
done

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║     ARCHER TRUCK AI — SETUP      ║"
echo "  ╚══════════════════════════════════╝"
echo ""

# ── Resolve + confirm the exact commit before touching anything ────
RESOLVED_SHA=$(git ls-remote "$REPO" "$REF" 2>/dev/null | awk '{print $1}' | head -n1)
if [ -z "$RESOLVED_SHA" ]; then
    # REF didn't resolve as a branch/tag on the remote — assume it's
    # already a commit SHA (git clone will fail below if it's not).
    RESOLVED_SHA="$REF"
fi
echo "  This will install code at commit: $RESOLVED_SHA"
echo "  (ref: $REF)"
if [ "$ASSUME_YES" != "true" ]; then
    if [ -r /dev/tty ]; then
        printf "  Continue? [y/N] "
        read -r CONFIRM </dev/tty
    else
        CONFIRM="n"
    fi
    case "$CONFIRM" in
        y|Y) ;;
        *)
            echo "  Aborted. Re-run with --yes to skip this prompt (unattended installs)."
            exit 1
            ;;
    esac
fi
echo ""

# ── System packages ──────────────────────────────────────
echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    ffmpeg git curl alsa-utils \
    avahi-daemon  # zero-conf: archer.local on the network

# ── Archer user ──────────────────────────────────────────
echo "[2/5] Creating archer user..."
useradd -m -s /bin/bash $ARCHER_USER 2>/dev/null || true
usermod -aG audio $ARCHER_USER

# ── Clone repo ───────────────────────────────────────────
echo "[3/5] Cloning Archer..."
if [ -d "$ARCHER_DIR/.git" ]; then
    git -C "$ARCHER_DIR" fetch origin "$REF" --quiet
    git -C "$ARCHER_DIR" reset --hard FETCH_HEAD --quiet
else
    git clone --no-checkout --depth 1 "$REPO" "$ARCHER_DIR" --quiet
    git -C "$ARCHER_DIR" fetch --depth 1 origin "$REF" --quiet
    git -C "$ARCHER_DIR" checkout --quiet FETCH_HEAD
fi
chown -R $ARCHER_USER:$ARCHER_USER "$ARCHER_DIR"

# ── Python deps in venv ──────────────────────────────────
echo "[4/5] Installing Python dependencies..."
sudo -u $ARCHER_USER python3 -m venv "$ARCHER_DIR/.venv"
sudo -u $ARCHER_USER "$ARCHER_DIR/.venv/bin/pip" install -q \
    flask edge-tts SpeechRecognition requests pyserial

# ── Systemd service ──────────────────────────────────────
echo "[5/5] Setting up autostart service..."
cp "$ARCHER_DIR/usb-os/archer.service" /etc/systemd/system/archer.service
sed -i "s|__ARCHER_DIR__|$ARCHER_DIR|g" /etc/systemd/system/archer.service
sed -i "s|__ARCHER_USER__|$ARCHER_USER|g" /etc/systemd/system/archer.service
sed -i "s|__ARCHER_PORT__|$ARCHER_PORT|g" /etc/systemd/system/archer.service

systemctl daemon-reload
systemctl enable archer --quiet
systemctl restart archer

# ── Firewall ─────────────────────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow $ARCHER_PORT/tcp --quiet 2>/dev/null || true
fi

# ── IP address display ───────────────────────────────────
sleep 2
LOCAL_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║           ARCHER IS RUNNING                  ║"
echo "  ║                                              ║"
echo "  ║  Local:   http://$LOCAL_IP:$ARCHER_PORT          ║"
echo "  ║  Network: http://archer.local:$ARCHER_PORT       ║"
echo "  ║                                              ║"
echo "  ║  Point your phone APK to the Local URL       ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
echo "  Logs: sudo journalctl -u archer -f"
echo ""
