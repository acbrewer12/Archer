#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  ARCHER OS — Custom Linux Image Builder
#  Produces: archer-os.img  (flash to USB with Balena Etcher)
#
#  Run on Ubuntu 22.04+ with sudo:
#    sudo bash build.sh
# ═══════════════════════════════════════════════════════════════
set -e

IMG="archer-os.img"
IMG_SIZE_MB=7168          # 7 GB — fits on 8 GB+ USB
ARCHER_REPO="https://github.com/acbrewer12/Archer.git"
ARCHER_BRANCH="claude/archer-truck-ai-system-TlfGE"
DEBIAN_RELEASE="bookworm"
WORK="$(pwd)/.build"
ROOTFS="$WORK/rootfs"
MOUNT="$WORK/mnt"

RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
log() { echo -e "${BOLD}[ARCHER OS]${NC} $1"; }
die() { echo -e "${RED}ERROR: $1${NC}"; exit 1; }

[ "$EUID" -eq 0 ] || die "Run with sudo"

# ── 1. Dependencies ──────────────────────────────────────────────
log "Installing build tools..."
apt-get update -qq
apt-get install -y -qq \
    debootstrap parted kpartx \
    grub-pc-bin grub-efi-amd64-bin grub2-common \
    dosfstools e2fsprogs \
    python3 python3-pip git curl squashfs-tools

# ── 2. Create disk image ─────────────────────────────────────────
log "Creating ${IMG_SIZE_MB}MB disk image..."
rm -rf "$WORK"
mkdir -p "$ROOTFS" "$MOUNT"
dd if=/dev/zero of="$IMG" bs=1M count=$IMG_SIZE_MB status=progress 2>&1 | tail -1

# ── 3. Partition: 1MB BIOS + 100MB EFI + rest root ──────────────
log "Partitioning image..."
parted -s "$IMG" \
    mklabel gpt \
    mkpart bios_boot 1MiB 2MiB \
    set 1 bios_grub on \
    mkpart EFI fat32 2MiB 102MiB \
    set 2 esp on \
    mkpart root ext4 102MiB 100%

LOOP=$(losetup -fP --show "$IMG")
log "Loop device: $LOOP"

mkfs.fat -F32 -n EFI   "${LOOP}p2" >/dev/null
mkfs.ext4 -L ARCHER_OS "${LOOP}p3" -q

mount "${LOOP}p3" "$MOUNT"
mkdir -p "$MOUNT/boot/efi"
mount "${LOOP}p2" "$MOUNT/boot/efi"

# ── 4. Bootstrap minimal Debian ──────────────────────────────────
log "Bootstrapping Debian $DEBIAN_RELEASE (this takes ~5 minutes)..."
debootstrap \
    --arch=amd64 \
    --include=systemd,systemd-sysv,udev,linux-image-amd64,grub-pc,grub-efi-amd64,\
python3,python3-pip,python3-venv,ffmpeg,git,curl,alsa-utils,\
network-manager,avahi-daemon,openssh-server \
    --exclude=man-db,manpages,info,vim-common,nano \
    "$DEBIAN_RELEASE" "$MOUNT" http://deb.debian.org/debian

# ── 5. System configuration ──────────────────────────────────────
log "Configuring Archer OS..."

# Hostname
echo "archer" > "$MOUNT/etc/hostname"
cat > "$MOUNT/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   archer archer.local
EOF

# fstab
ROOT_UUID=$(blkid -s UUID -o value "${LOOP}p3")
EFI_UUID=$(blkid -s UUID -o value "${LOOP}p2")
cat > "$MOUNT/etc/fstab" <<EOF
UUID=$ROOT_UUID  /          ext4  errors=remount-ro  0 1
UUID=$EFI_UUID   /boot/efi  vfat  umask=0077         0 2
EOF

# Locale + timezone
echo "LANG=en_US.UTF-8" > "$MOUNT/etc/default/locale"
ln -sf /usr/share/zoneinfo/America/Chicago "$MOUNT/etc/localtime"

# NetworkManager manages WiFi
mkdir -p "$MOUNT/etc/NetworkManager"
cat > "$MOUNT/etc/NetworkManager/NetworkManager.conf" <<EOF
[main]
plugins=keyfile
[device]
wifi.scan-rand-mac-address=no
EOF

# Auto-login as archer on tty1 (no password prompt)
mkdir -p "$MOUNT/etc/systemd/system/getty@tty1.service.d"
cat > "$MOUNT/etc/systemd/system/getty@tty1.service.d/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin archer --noclear %I $TERM
EOF

# ── 6. Archer user + files ───────────────────────────────────────
log "Setting up Archer user and files..."
chroot "$MOUNT" useradd -m -s /bin/bash archer
chroot "$MOUNT" usermod -aG audio,video,dialout archer

# Clone Archer into the OS
chroot "$MOUNT" git clone --branch "$ARCHER_BRANCH" --depth 1 \
    "$ARCHER_REPO" /opt/archer

# Python venv + deps
chroot "$MOUNT" python3 -m venv /opt/archer/.venv
chroot "$MOUNT" /opt/archer/.venv/bin/pip install -q \
    flask edge-tts SpeechRecognition requests pyserial

chroot "$MOUNT" chown -R archer:archer /opt/archer

# ── 7. Archer systemd service ────────────────────────────────────
log "Installing Archer service..."
cp "$(dirname "$0")/overlay/etc/systemd/system/archer.service" \
    "$MOUNT/etc/systemd/system/archer.service"

chroot "$MOUNT" systemctl enable archer
chroot "$MOUNT" systemctl enable NetworkManager
chroot "$MOUNT" systemctl enable avahi-daemon

# Disable unneeded services for speed
chroot "$MOUNT" systemctl disable ssh 2>/dev/null || true

# ── 8. Boot splash + GRUB ───────────────────────────────────────
log "Configuring GRUB bootloader..."
cp "$(dirname "$0")/config/grub.cfg" "$MOUNT/etc/grub.d/40_archer"
chmod +x "$MOUNT/etc/grub.d/40_archer"

cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash loglevel=0"
GRUB_CMDLINE_LINUX=""
GRUB_TERMINAL=console
EOF

# Install GRUB for both UEFI and legacy BIOS
chroot "$MOUNT" grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi --bootloader-id=ARCHER \
    --removable --no-nvram 2>/dev/null
chroot "$MOUNT" grub-install --target=i386-pc \
    "$LOOP" 2>/dev/null
chroot "$MOUNT" update-grub 2>/dev/null

# ── 9. MOTD / status screen ─────────────────────────────────────
cat > "$MOUNT/etc/motd" <<'EOF'

  ╔═══════════════════════════════════════╗
  ║          ARCHER TRUCK AI OS           ║
  ╚═══════════════════════════════════════╝

  Service:  sudo systemctl status archer
  Logs:     sudo journalctl -u archer -f
  Update:   sudo /opt/archer/usb-os/update.sh

EOF

# ── 10. Cleanup + unmount ────────────────────────────────────────
log "Finalizing image..."
umount "$MOUNT/boot/efi"
umount "$MOUNT"
losetup -d "$LOOP"
rm -rf "$WORK"

# Compress
log "Compressing image..."
gzip -k "$IMG"

echo ""
echo -e "  ${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "  ${BOLD}║       ARCHER OS BUILD COMPLETE           ║${NC}"
echo -e "  ${BOLD}║                                          ║${NC}"
echo -e "  ${BOLD}║  Image: $(pwd)/$IMG        ║${NC}"
echo -e "  ${BOLD}║                                          ║${NC}"
echo -e "  ${BOLD}║  Flash to USB:                           ║${NC}"
echo -e "  ${BOLD}║  → Balena Etcher (Windows/Mac/Linux)     ║${NC}"
echo -e "  ${BOLD}║  → Open archer-os.img — select USB       ║${NC}"
echo -e "  ${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""
