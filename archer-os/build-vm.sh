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

# ══ PRE-FLIGHT CHECKS ════════════════════════════════════════════
echo ""
echo -e "  ${BOLD}${CYAN}═══ ARCHER OS VM PRE-FLIGHT CHECKS ═══${NC}"
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

# Check 3: Disk space ≥8GB free in current directory
AVAIL_KB=$(df -k . | awk 'NR==2{print $4}')
AVAIL_GB=$(( AVAIL_KB / 1024 / 1024 ))
if [ "$AVAIL_GB" -lt 8 ]; then
    echo -e "  ${RED}✗${NC} Insufficient disk space: ${AVAIL_GB}GB available, ${BOLD}8GB required${NC}"
    PREFLIGHT_OK=false
else
    echo -e "  ${GREEN}✓${NC} Disk space: ${AVAIL_GB}GB available (required: 8GB)"
fi

# Check 4: RAM ≥2GB
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$(( TOTAL_RAM_KB / 1024 / 1024 ))
if [ "$TOTAL_RAM_GB" -lt 2 ]; then
    echo -e "  ${RED}✗${NC} Insufficient RAM: ${TOTAL_RAM_GB}GB available, ${BOLD}2GB required${NC}"
    PREFLIGHT_OK=false
else
    echo -e "  ${GREEN}✓${NC} RAM: ${TOTAL_RAM_GB}GB available (required: 2GB)"
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
echo -e "  ${GREEN}${BOLD}All pre-flight checks passed. Starting VM build...${NC}"
echo ""
# ══ END PRE-FLIGHT ════════════════════════════════════════════════

step "Installing build tools..."
apt-get update -qq
apt-get install -y -qq \
    debootstrap parted kpartx \
    grub-pc-bin grub-efi-amd64-bin grub2-common \
    dosfstools e2fsprogs git curl python3 python3-pip python3-venv \
    gcc libc6-dev make qemu-utils

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
ffmpeg,git,curl \
    --exclude=man-db,manpages,info \
    "$DEBIAN_RELEASE" "$MOUNT" http://deb.debian.org/debian

# Mount virtual filesystems — kept mounted for all subsequent chroot operations.
# grub-install, systemctl, and apt postinstall scripts all need these.
mount --bind /proc    "$MOUNT/proc"
mount --bind /sys     "$MOUNT/sys"
mount --bind /dev     "$MOUNT/dev"
mount --bind /dev/pts "$MOUNT/dev/pts"

# policy-rc.d returning 101 prevents service start attempts during chroot apt-get.
# Without this, NetworkManager/avahi postinstall scripts try to run systemctl
# which fails (no running systemd in the chroot) and aborts the install.
cat > "$MOUNT/usr/sbin/policy-rc.d" <<'POLICY'
#!/bin/sh
exit 101
POLICY
chmod +x "$MOUNT/usr/sbin/policy-rc.d"

DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq \
    network-manager avahi-daemon dbus sudo

# Remove policy override — on real boot services start normally
rm -f "$MOUNT/usr/sbin/policy-rc.d"

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
chroot "$MOUNT" usermod -aG audio,dialout,sudo archer

# Set root password to 'archer' for console debugging
chroot "$MOUNT" bash -c "echo 'root:archer' | chpasswd"

# Give archer passwordless sudo for console convenience
mkdir -p "$MOUNT/etc/sudoers.d"
echo "archer ALL=(ALL) NOPASSWD:ALL" > "$MOUNT/etc/sudoers.d/archer"
chmod 440 "$MOUNT/etc/sudoers.d/archer"

mkdir -p "$MOUNT/etc/systemd/system/getty@tty1.service.d"
cat > "$MOUNT/etc/systemd/system/getty@tty1.service.d/autologin.conf" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin archer --noclear %I \$TERM
EOF

step "Installing Archer source and Python deps..."
mkdir -p "$MOUNT/opt/archer"
if [ -n "$ARCHER_LOCAL_SRC" ] && [ -d "$ARCHER_LOCAL_SRC/.git" ]; then
    # CI: extract only git-tracked files — skips .git dir and build artifacts
    # (archer-os.img etc. would fill the 4GB image)
    log "Using git archive from: $ARCHER_LOCAL_SRC"
    git -C "$ARCHER_LOCAL_SRC" archive HEAD | tar -x -C "$MOUNT/opt/archer"
else
    log "Cloning from GitHub..."
    git clone \
        --branch "$ARCHER_BRANCH" --depth 1 \
        "$ARCHER_REPO" "$MOUNT/opt/archer"
fi

# pip needs network — copy host resolv.conf temporarily so pip can reach PyPI
cp /etc/resolv.conf "$MOUNT/etc/resolv.conf"

chroot "$MOUNT" python3 -m venv /opt/archer/.venv
chroot "$MOUNT" /opt/archer/.venv/bin/pip install -q \
    flask edge-tts SpeechRecognition requests pyserial

# Remove the host resolv.conf — the OS will get its own DNS from NetworkManager
rm -f "$MOUNT/etc/resolv.conf"
chroot "$MOUNT" chown -R archer:archer /opt/archer

step "Compiling archer_init (custom PID 1 — replaces systemd)..."
gcc -static -Os -Wall -std=c11 -D_GNU_SOURCE \
    -o "$MOUNT/sbin/archer_init" \
    "$(dirname "$0")/init/archer_init.c"
chmod 755 "$MOUNT/sbin/archer_init"
log "archer_init installed ($(stat -c%s "$MOUNT/sbin/archer_init") bytes)"

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
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=0 init=/sbin/archer_init"
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

step "Unmounting and converting VM image..."
# Tear down bind mounts before unmounting the image filesystem
umount "$MOUNT/dev/pts" 2>/dev/null || true
umount "$MOUNT/dev"     2>/dev/null || true
umount "$MOUNT/sys"     2>/dev/null || true
umount "$MOUNT/proc"    2>/dev/null || true
umount "$MOUNT/boot/efi"
umount "$MOUNT"
losetup -d "$LOOP"
rm -rf "$WORK"

# Convert raw image to VMDK for VMware Workstation Pro
if command -v qemu-img &>/dev/null; then
    log "Converting to VMDK for VMware Workstation Pro..."
    qemu-img convert -f raw -O vmdk -o subformat=monolithicSparse "$IMG" "${IMG%.img}.vmdk"
    VMDK_SIZE=$(( $(stat -c%s "${IMG%.img}.vmdk" 2>/dev/null || echo "0") / 1024 / 1024 ))
    log "VMDK created: ${IMG%.img}.vmdk (${VMDK_SIZE} MB)"
else
    log "qemu-img not found — skipping VMDK conversion (install qemu-utils to get it)"
fi

# Also keep a compressed raw image as backup
gzip -f "$IMG"

# ── Final stats ───────────────────────────────────────────────────
BUILD_END=$(date +%s)
ELAPSED=$(( BUILD_END - BUILD_START ))
ELAPSED_FMT="${ELAPSED}s"
[ $ELAPSED -ge 60 ] && ELAPSED_FMT="$((ELAPSED/60))m $((ELAPSED%60))s"

VMDK="${IMG%.img}.vmdk"
VMDK_SIZE_MB=$(( $(stat -c%s "$VMDK" 2>/dev/null || echo "0") / 1024 / 1024 ))
IMG_GZ="${IMG}.gz"
IMG_GZ_SIZE=$(( $(stat -c%s "$IMG_GZ" 2>/dev/null || echo "0") / 1024 / 1024 ))

echo -e "\n${GREEN}  Computing SHA256 checksums...${NC}"
SHA256_VMDK=$(sha256sum "$VMDK" 2>/dev/null | awk '{print $1}')
SHA256_GZ=$(sha256sum "$IMG_GZ" 2>/dev/null | awk '{print $1}')
[ -n "$SHA256_VMDK" ] && echo "$SHA256_VMDK  $VMDK" > "${VMDK}.sha256"
echo "$SHA256_GZ  $IMG_GZ" > "${IMG_GZ}.sha256"

echo ""
echo -e "  ${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "  ${BOLD}${GREEN}║        ARCHER OS VM BUILD COMPLETE                   ║${NC}"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  VMDK:    ${BOLD}$(pwd)/${VMDK}${NC}  (${VMDK_SIZE_MB} MB)"
echo -e "  ${BOLD}${GREEN}║${NC}  RAW.GZ:  ${BOLD}$(pwd)/${IMG_GZ}${NC}  (${IMG_GZ_SIZE} MB)"
echo -e "  ${BOLD}${GREEN}║${NC}  Elapsed: ${BOLD}${ELAPSED_FMT}${NC}"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  VMware Workstation Pro:"
echo -e "  ${BOLD}${GREEN}║${NC}    1. File → New Virtual Machine → Custom"
echo -e "  ${BOLD}${GREEN}║${NC}    2. Hardware compatibility → Workstation 17"
echo -e "  ${BOLD}${GREEN}║${NC}    3. 'I will install OS later' → Linux → Debian 12 64-bit"
echo -e "  ${BOLD}${GREEN}║${NC}    4. RAM: 2048 MB minimum, CPUs: 2"
echo -e "  ${BOLD}${GREEN}║${NC}    5. Disk → Use an existing virtual disk → archer-os.vmdk"
echo -e "  ${BOLD}${GREEN}║${NC}    6. Power on → Archer boots in ~5 seconds"
echo -e "  ${BOLD}${GREEN}║${NC}    7. Get IP:  ip addr show  (or check DHCP leases)"
echo -e "  ${BOLD}${GREEN}║${NC}    8. Open:    http://<VM_IP>:5000"
echo -e "  ${BOLD}${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}${GREEN}║${NC}  Verify archer_init is PID 1 (in VM terminal):"
echo -e "  ${BOLD}${GREEN}║${NC}    ps aux | head -5"
echo -e "  ${BOLD}${GREEN}║${NC}    cat /run/archer_init.log"
echo -e "  ${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
