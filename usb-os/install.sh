#!/bin/bash
# ═══════════════════════════════════════════════════════
#  ARCHER USB OS — One-shot installer
#  Run on a fresh Ubuntu Server boot:
#    curl -fsSL https://raw.githubusercontent.com/acbrewer12/Archer/claude/archer-truck-ai-system-TlfGE/usb-os/install.sh | sudo bash
# ═══════════════════════════════════════════════════════
set -e

ARCHER_DIR="/opt/archer"
ARCHER_PORT=5000
ARCHER_USER="archer"
REPO="https://github.com/acbrewer12/Archer.git"
BRANCH="claude/archer-truck-ai-system-TlfGE"

echo ""
echo "  ╔══════════════════════════════════╗"
echo "  ║     ARCHER TRUCK AI — SETUP      ║"
echo "  ╚══════════════════════════════════╝"
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
    git -C "$ARCHER_DIR" pull origin "$BRANCH" --quiet
else
    git clone --branch "$BRANCH" --depth 1 "$REPO" "$ARCHER_DIR" --quiet
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
