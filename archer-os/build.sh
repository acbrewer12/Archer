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

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
BUILD_START=$(date +%s)
STEP_CURRENT=0
STEP_TOTAL=10

log() { echo -e "${BOLD}[ARCHER OS]${NC} $1"; }
die() { echo -e "${RED}ERROR: $1${NC}"; exit 1; }

step() {
    STEP_CURRENT=$((STEP_CURRENT + 1))
    local pct=$(( (STEP_CURRENT * 100) / STEP_TOTAL ))
    local elapsed=$(( $(date +%s) - BUILD_START ))
    local elapsed_fmt="${elapsed}s"
    [ $elapsed -ge 60 ] && elapsed_fmt="$((elapsed/60))m $((elapsed%60))s"
    # Progress bar (40 chars wide)
    local filled=$(( pct * 40 / 100 ))
    local bar=""
    for i in $(seq 1 $filled);   do bar="${bar}█"; done
    for i in $(seq $((filled+1)) 40); do bar="${bar}░"; done
    echo ""
    echo -e "  ${CYAN}${bar}${NC} ${BOLD}${pct}%${NC}  [${STEP_CURRENT}/${STEP_TOTAL}]  ${YELLOW}+${elapsed_fmt}${NC}"
    echo -e "  ${BOLD}▸ $1${NC}"
}

[ "$EUID" -eq 0 ] || die "Run with sudo"

# ══ PRE-FLIGHT CHECKS ════════════════════════════════════════════
echo ""
echo -e "  ${BOLD}${CYAN}═══ ARCHER OS PRE-FLIGHT CHECKS ═══${NC}"
echo ""
PREFLIGHT_OK=true

# Check 1: Root
echo -e "  ${GREEN}✓${NC} Running as root"

# Check 2: Required commands
REQUIRED_CMDS="debootstrap parted losetup mkfs.fat mkfs.ext4 grub-install git curl python3 gzip sha256sum stat"
for cmd in $REQUIRED_CMDS; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "  ${YELLOW}!${NC} Missing command: ${BOLD}${cmd}${NC} (will be installed)"
    fi
done
echo -e "  ${GREEN}✓${NC} Command availability checked"

# Check 3: Disk space ≥15GB free in current directory
AVAIL_KB=$(df -k . | awk 'NR==2{print $4}')
AVAIL_GB=$(( AVAIL_KB / 1024 / 1024 ))
if [ "$AVAIL_GB" -lt 15 ]; then
    echo -e "  ${RED}✗${NC} Insufficient disk space: ${AVAIL_GB}GB available, ${BOLD}15GB required${NC}"
    PREFLIGHT_OK=false
else
    echo -e "  ${GREEN}✓${NC} Disk space: ${AVAIL_GB}GB available (required: 15GB)"
fi

# Check 4: RAM ≥4GB
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$(( TOTAL_RAM_KB / 1024 / 1024 ))
if [ "$TOTAL_RAM_GB" -lt 4 ]; then
    echo -e "  ${RED}✗${NC} Insufficient RAM: ${TOTAL_RAM_GB}GB available, ${BOLD}4GB required${NC}"
    PREFLIGHT_OK=false
else
    echo -e "  ${GREEN}✓${NC} RAM: ${TOTAL_RAM_GB}GB available (required: 4GB)"
fi

# Check 5: Internet connectivity (ping Debian mirror)
if curl -s --max-time 5 http://deb.debian.org/debian/dists/bookworm/Release -o /dev/null; then
    echo -e "  ${GREEN}✓${NC} Internet connectivity: Debian mirror reachable"
else
    echo -e "  ${RED}✗${NC} Cannot reach deb.debian.org — check internet connection"
    PREFLIGHT_OK=false
fi

# Check 6: OS version (Ubuntu/Debian recommended)
OS_ID=$(. /etc/os-release 2>/dev/null && echo "$ID")
OS_VER=$(. /etc/os-release 2>/dev/null && echo "$VERSION_ID")
if [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
    echo -e "  ${GREEN}✓${NC} Host OS: ${OS_ID} ${OS_VER}"
else
    echo -e "  ${YELLOW}!${NC} Host OS: ${OS_ID} ${OS_VER} — Ubuntu 22.04+ recommended"
fi

echo ""
if [ "$PREFLIGHT_OK" != "true" ]; then
    echo -e "  ${RED}${BOLD}Pre-flight checks FAILED. Fix the issues above and re-run.${NC}"
    echo ""
    exit 1
fi
echo -e "  ${GREEN}${BOLD}All pre-flight checks passed. Starting build...${NC}"
echo ""
# ══ END PRE-FLIGHT ════════════════════════════════════════════════

# ── 1. Dependencies ──────────────────────────────────────────────
step "Installing build tools..."
apt-get update -qq
apt-get install -y -qq \
    debootstrap parted kpartx \
    grub-pc-bin grub-efi-amd64-bin grub2-common \
    dosfstools e2fsprogs \
    python3 python3-pip git curl squashfs-tools \
    gcc libc6-dev make

# ── 2. Create disk image ─────────────────────────────────────────
step "Creating ${IMG_SIZE_MB}MB disk image..."
rm -rf "$WORK"
mkdir -p "$ROOTFS" "$MOUNT"
dd if=/dev/zero of="$IMG" bs=1M count=$IMG_SIZE_MB status=progress 2>&1 | tail -1

# ── 3. Partition: 1MB BIOS + 100MB EFI + rest root ──────────────
step "Partitioning image and formatting filesystems..."
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
step "Bootstrapping Debian $DEBIAN_RELEASE (this takes ~5 minutes)..."
debootstrap \
    --arch=amd64 \
    --include=systemd,systemd-sysv,udev,linux-image-amd64,grub-pc,grub-efi-amd64,\
python3,python3-pip,python3-venv,ffmpeg,git,curl,alsa-utils \
    --exclude=man-db,manpages,info,vim-common,nano \
    "$DEBIAN_RELEASE" "$MOUNT" http://deb.debian.org/debian

# Mount virtual filesystems so chroot apt-get postinstall scripts work
mount --bind /proc    "$MOUNT/proc"
mount --bind /sys     "$MOUNT/sys"
mount --bind /dev     "$MOUNT/dev"
mount --bind /dev/pts "$MOUNT/dev/pts"

cat > "$MOUNT/usr/sbin/policy-rc.d" <<'POLICY'
#!/bin/sh
exit 101
POLICY
chmod +x "$MOUNT/usr/sbin/policy-rc.d"

DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq \
    network-manager avahi-daemon dbus openssh-server

rm -f "$MOUNT/usr/sbin/policy-rc.d"

# ── 5. System configuration ──────────────────────────────────────
step "Configuring Archer OS hostname, locale, and networking..."

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
step "Setting up Archer user, cloning repo, installing Python deps..."
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

# ── 7. Compile and install Archer custom init (PID 1) ────────────
step "Compiling archer_init (custom PID 1 — replaces systemd)..."
# Compile statically on the build host — no deps needed in the target image
gcc -static -Os -Wall -std=c11 -D_GNU_SOURCE \
    -o "$MOUNT/sbin/archer_init" \
    "$(dirname "$0")/init/archer_init.c"
chmod 755 "$MOUNT/sbin/archer_init"

# Tell GRUB to use our init instead of systemd
# This is set below in the GRUB config step — kept here as a note
log "archer_init installed at /sbin/archer_init ($(stat -c%s "$MOUNT/sbin/archer_init") bytes)"

# ── 8. Archer systemd service ────────────────────────────────────
step "Installing Archer systemd service and enabling services..."
# We still install the systemd service as a fallback (if init= is removed from cmdline)
cp "$(dirname "$0")/overlay/etc/systemd/system/archer.service" \
    "$MOUNT/etc/systemd/system/archer.service"

chroot "$MOUNT" systemctl enable archer
chroot "$MOUNT" systemctl enable NetworkManager
chroot "$MOUNT" systemctl enable avahi-daemon

# Disable unneeded services for speed
chroot "$MOUNT" systemctl disable ssh 2>/dev/null || true

# ── 8. Boot splash + GRUB ───────────────────────────────────────
step "Configuring GRUB bootloader (UEFI + Legacy BIOS)..."
cp "$(dirname "$0")/config/grub.cfg" "$MOUNT/etc/grub.d/40_archer"
chmod +x "$MOUNT/etc/grub.d/40_archer"

cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=0 init=/sbin/archer_init"
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

# ── 9. MOTD already done above, this is step 9 ──────────────────
step "Writing MOTD and finalizing filesystem..."

# ── 10. Cleanup + unmount ────────────────────────────────────────
umount "$MOUNT/dev/pts" 2>/dev/null || true
umount "$MOUNT/dev"     2>/dev/null || true
umount "$MOUNT/sys"     2>/dev/null || true
umount "$MOUNT/proc"    2>/dev/null || true
umount "$MOUNT/boot/efi"
umount "$MOUNT"
losetup -d "$LOOP"
rm -rf "$WORK"

# Compress
step "Compressing image with gzip..."
gzip -k "$IMG"

# ── Final stats ───────────────────────────────────────────────────
BUILD_END=$(date +%s)
ELAPSED=$(( BUILD_END - BUILD_START ))
ELAPSED_FMT="${ELAPSED}s"
[ $ELAPSED -ge 60 ] && ELAPSED_FMT="$((ELAPSED/60))m $((ELAPSED%60))s"

IMG_SIZE_BYTES=$(stat -c%s "$IMG" 2>/dev/null || echo "0")
IMG_SIZE_MB_ACTUAL=$(( IMG_SIZE_BYTES / 1024 / 1024 ))
IMG_SIZE_GZ_MB=$(( $(stat -c%s "${IMG}.gz" 2>/dev/null || echo "0") / 1024 / 1024 ))

echo -e "\n${GREEN}  Computing SHA256 checksum...${NC}"
SHA256=$(sha256sum "$IMG" | awk '{print $1}')
# Save checksum file
echo "$SHA256  $IMG" > "${IMG}.sha256"
echo -e "  SHA256 saved to: ${IMG}.sha256\n"

echo -e "  ${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "  ${BOLD}${GREEN}║        ARCHER OS BUILD COMPLETE                      ║${NC}"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  Image:    ${BOLD}$(pwd)/${IMG}${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  Size:     ${BOLD}${IMG_SIZE_MB_ACTUAL} MB${NC}  (compressed: ${IMG_SIZE_GZ_MB} MB)"
echo -e "  ${BOLD}${GREEN}║${NC}  SHA256:   ${SHA256:0:32}..."
echo -e "  ${BOLD}${GREEN}║${NC}  Elapsed:  ${BOLD}${ELAPSED_FMT}${NC}"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  Flash to USB:"
echo -e "  ${BOLD}${GREEN}║${NC}  → Balena Etcher (Windows / Mac / Linux)"
echo -e "  ${BOLD}${GREEN}║${NC}  → Open archer-os.img — select USB drive"
echo -e "  ${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
