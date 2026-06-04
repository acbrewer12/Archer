#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  ARCHER OS — Lightweight VM image builder (for VirtualBox/VMware)
#  Produces: archer-os.img  (~1.5GB compressed)
#
#  Run on Ubuntu 22.04+ with sudo:
#    sudo bash build-vm.sh
# ═══════════════════════════════════════════════════════════════
set -e

IMG="archer-os.img"
IMG_SIZE_MB=4096          # 4GB — enough for VM testing
ARCHER_REPO="https://github.com/acbrewer12/Archer.git"
ARCHER_BRANCH="claude/archer-truck-ai-system-TlfGE"
DEBIAN_RELEASE="bookworm"
WORK="$(pwd)/.build"
MOUNT="$WORK/mnt"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
BUILD_START=$(date +%s)
STEP_CURRENT=0
STEP_TOTAL=9

log() { echo -e "${BOLD}[ARCHER OS]${NC} $1"; }
die() { echo -e "${RED}ERROR: $1${NC}"; exit 1; }

step() {
    STEP_CURRENT=$((STEP_CURRENT + 1))
    local pct=$(( (STEP_CURRENT * 100) / STEP_TOTAL ))
    local elapsed=$(( $(date +%s) - BUILD_START ))
    local elapsed_fmt="${elapsed}s"
    [ $elapsed -ge 60 ] && elapsed_fmt="$((elapsed/60))m $((elapsed%60))s"
    local filled=$(( pct * 40 / 100 ))
    local bar=""
    for i in $(seq 1 $filled);        do bar="${bar}█"; done
    for i in $(seq $((filled+1)) 40); do bar="${bar}░"; done
    echo ""
    echo -e "  ${CYAN}${bar}${NC} ${BOLD}${pct}%${NC}  [${STEP_CURRENT}/${STEP_TOTAL}]  ${YELLOW}+${elapsed_fmt}${NC}"
    echo -e "  ${BOLD}▸ $1${NC}"
}

[ "$EUID" -eq 0 ] || die "Run with sudo"

step "Installing build tools..."
apt-get update -qq
apt-get install -y -qq \
    debootstrap parted kpartx \
    grub-pc-bin grub-efi-amd64-bin grub2-common \
    dosfstools e2fsprogs git curl python3 python3-pip python3-venv

step "Creating ${IMG_SIZE_MB}MB disk image..."
rm -rf "$WORK" "$IMG"
mkdir -p "$MOUNT"
dd if=/dev/zero of="$IMG" bs=1M count=$IMG_SIZE_MB status=none

step "Partitioning and formatting filesystems..."
parted -s "$IMG" \
    mklabel gpt \
    mkpart bios_boot 1MiB 2MiB  set 1 bios_grub on \
    mkpart EFI fat32 2MiB 102MiB set 2 esp on \
    mkpart root ext4 102MiB 100%

LOOP=$(losetup -fP --show "$IMG")
mkfs.fat -F32 -n EFI    "${LOOP}p2" >/dev/null
mkfs.ext4 -L ARCHER_OS  "${LOOP}p3" -q

mount "${LOOP}p3" "$MOUNT"
mkdir -p "$MOUNT/boot/efi"
mount "${LOOP}p2" "$MOUNT/boot/efi"

step "Bootstrapping Debian $DEBIAN_RELEASE (~5 min)..."
debootstrap \
    --arch=amd64 \
    --include=systemd,systemd-sysv,udev,linux-image-amd64,\
grub-pc,grub-efi-amd64,python3,python3-pip,python3-venv,\
ffmpeg,git,curl,network-manager,avahi-daemon \
    --exclude=man-db,manpages,info \
    "$DEBIAN_RELEASE" "$MOUNT" http://deb.debian.org/debian

step "Configuring Archer OS system settings..."
echo "archer" > "$MOUNT/etc/hostname"
cat > "$MOUNT/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   archer archer.local
EOF

ROOT_UUID=$(blkid -s UUID -o value "${LOOP}p3")
EFI_UUID=$(blkid  -s UUID -o value "${LOOP}p2")
cat > "$MOUNT/etc/fstab" <<EOF
UUID=$ROOT_UUID  /          ext4  errors=remount-ro  0 1
UUID=$EFI_UUID   /boot/efi  vfat  umask=0077         0 2
EOF

echo "LANG=en_US.UTF-8" > "$MOUNT/etc/default/locale"
ln -sf /usr/share/zoneinfo/America/Chicago "$MOUNT/etc/localtime"

# Auto-login archer user on tty1
chroot "$MOUNT" useradd -m -s /bin/bash archer
chroot "$MOUNT" usermod -aG audio,dialout archer

mkdir -p "$MOUNT/etc/systemd/system/getty@tty1.service.d"
cat > "$MOUNT/etc/systemd/system/getty@tty1.service.d/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin archer --noclear %I \$TERM
EOF

step "Cloning Archer repo and installing Python deps..."
chroot "$MOUNT" git clone \
    --branch "$ARCHER_BRANCH" --depth 1 \
    "$ARCHER_REPO" /opt/archer

chroot "$MOUNT" python3 -m venv /opt/archer/.venv
chroot "$MOUNT" /opt/archer/.venv/bin/pip install -q \
    flask edge-tts SpeechRecognition requests pyserial
chroot "$MOUNT" chown -R archer:archer /opt/archer

step "Installing Archer systemd service and enabling services..."
cp "$(dirname "$0")/overlay/etc/systemd/system/archer.service" \
    "$MOUNT/etc/systemd/system/archer.service"
chroot "$MOUNT" systemctl enable archer
chroot "$MOUNT" systemctl enable NetworkManager
chroot "$MOUNT" systemctl enable avahi-daemon

step "Installing GRUB bootloader (UEFI + Legacy BIOS)..."
cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=0"
EOF

chroot "$MOUNT" grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi --bootloader-id=ARCHER \
    --removable --no-nvram >/dev/null 2>&1
chroot "$MOUNT" grub-install --target=i386-pc "$LOOP" >/dev/null 2>&1
chroot "$MOUNT" update-grub >/dev/null 2>&1

cat > "$MOUNT/etc/motd" <<'EOF'

  ╔═══════════════════════════════════╗
  ║       ARCHER TRUCK AI OS          ║
  ╚═══════════════════════════════════╝
  Logs:   journalctl -u archer -f
  Update: sudo /opt/archer/usb-os/update.sh

EOF

step "Unmounting and compressing VM image..."
umount "$MOUNT/boot/efi"
umount "$MOUNT"
losetup -d "$LOOP"
rm -rf "$WORK"

gzip -f "$IMG"

# ── Final stats ───────────────────────────────────────────────────
BUILD_END=$(date +%s)
ELAPSED=$(( BUILD_END - BUILD_START ))
ELAPSED_FMT="${ELAPSED}s"
[ $ELAPSED -ge 60 ] && ELAPSED_FMT="$((ELAPSED/60))m $((ELAPSED%60))s"

IMG_GZ="${IMG}.gz"
IMG_GZ_SIZE=$(( $(stat -c%s "$IMG_GZ" 2>/dev/null || echo "0") / 1024 / 1024 ))

echo -e "\n${GREEN}  Computing SHA256 checksum of compressed image...${NC}"
SHA256=$(sha256sum "$IMG_GZ" | awk '{print $1}')
echo "$SHA256  $IMG_GZ" > "${IMG_GZ}.sha256"

echo ""
echo -e "  ${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "  ${BOLD}${GREEN}║        ARCHER OS VM BUILD COMPLETE                   ║${NC}"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  Image:   ${BOLD}$(pwd)/${IMG_GZ}${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  Size:    ${BOLD}${IMG_GZ_SIZE} MB${NC} (compressed)"
echo -e "  ${BOLD}${GREEN}║${NC}  SHA256:  ${SHA256:0:32}..."
echo -e "  ${BOLD}${GREEN}║${NC}  Elapsed: ${BOLD}${ELAPSED_FMT}${NC}"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  VirtualBox:"
echo -e "  ${BOLD}${GREEN}║${NC}    1. New VM → Linux 64-bit → skip ISO"
echo -e "  ${BOLD}${GREEN}║${NC}    2. gunzip ${IMG_GZ} first"
echo -e "  ${BOLD}${GREEN}║${NC}    3. Storage → Add Disk → Use Existing → archer-os.img"
echo -e "  ${BOLD}${GREEN}║${NC}  VMware:"
echo -e "  ${BOLD}${GREEN}║${NC}    File → Open → archer-os.img.gz"
echo -e "  ${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
