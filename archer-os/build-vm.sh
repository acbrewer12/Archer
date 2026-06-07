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
STEP_TOTAL=12

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
    network-manager avahi-daemon dbus sudo isc-dhcp-client curl

# X11 kiosk — pre-register Xorg permissions so WSL2 setuid block doesn't abort
mkdir -p "$MOUNT/var/lib/dpkg"
# Tell dpkg not to set setuid on Xorg (0755 instead of 4755)
# archer user has NOPASSWD sudo so X starts via sudo wrapper instead
chroot "$MOUNT" bash -c "dpkg-statoverride --add root root 0755 /usr/bin/Xorg 2>/dev/null; true"
echo "force-unsafe-io" > "$MOUNT/etc/dpkg/dpkg.cfg.d/99archer-build"
DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq \
    --no-install-recommends \
    xorg xinit chromium x11-xserver-utils 2>&1 || \
    log "WARNING: X11/Chromium install had errors (kiosk may not work)"
rm -f "$MOUNT/etc/dpkg/dpkg.cfg.d/99archer-build"
# Allow non-root users to start X
mkdir -p "$MOUNT/etc/X11"
cat > "$MOUNT/etc/X11/Xwrapper.config" <<'XWRAP'
allowed_users=anybody
needs_root_rights=yes
XWRAP

# Force fbdev driver so Xorg works with our custom kernel (no udevd to load DRM modules).
# fbdev uses the kernel framebuffer — available at boot via CONFIG_FB_VESA=y / CONFIG_FB_EFI=y.
mkdir -p "$MOUNT/etc/X11/xorg.conf.d"
cat > "$MOUNT/etc/X11/xorg.conf.d/10-fbdev.conf" <<'XORGCONF'
Section "Device"
    Identifier  "Archer Display"
    Driver      "fbdev"
    Option      "fbdev" "/dev/fb0"
EndSection

Section "Screen"
    Identifier  "Archer Screen"
    Device      "Archer Display"
    DefaultDepth 24
EndSection
XORGCONF

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
chroot "$MOUNT" usermod -aG audio,dialout,sudo,video,input archer

# Set root password to 'archer' for console debugging
chroot "$MOUNT" bash -c "echo 'root:archer' | chpasswd"

# Give archer passwordless sudo for console convenience
mkdir -p "$MOUNT/etc/sudoers.d"
echo "archer ALL=(ALL) NOPASSWD:ALL" > "$MOUNT/etc/sudoers.d/archer"
chmod 440 "$MOUNT/etc/sudoers.d/archer"

# Kiosk launch script — waits for Flask, then opens Chromium fullscreen
mkdir -p "$MOUNT/opt/archer"
cat > "$MOUNT/opt/archer/kiosk.sh" <<'KIOSK'
#!/bin/bash
# GPU modules are loaded by archer_init (root) before this script runs.
# Create Xorg + Chromium profile directories — root is now rw thanks to
# archer_init's remount. A missing/unwritable profile dir can make Chromium
# hang silently on a fresh boot instead of erroring out.
mkdir -p /home/archer/.local/share/xorg      2>/dev/null || true
mkdir -p /home/archer/.config/archer-chrome  2>/dev/null || true
touch /home/archer/.Xauthority 2>/dev/null || true

# Wait up to 45s for Flask to be ready. Pure-bash TCP probe — curl is not
# guaranteed to be present this early (and isn't worth the dependency here).
for i in $(seq 1 45); do
    { exec 3<>/dev/tcp/127.0.0.1/5000; } 2>/dev/null && { exec 3<&- 3>&-; break; }
    sleep 1
done
# Disable screensaver / power management
xset s off -dpms 2>/dev/null || true

URL="http://127.0.0.1:5000/dashboard"
LOG=/tmp/archer-chromium.log
: > "$LOG"

CHROME_FLAGS=(
    --kiosk
    --no-sandbox
    --disable-infobars
    --no-first-run
    --disable-translate
    --disable-extensions
    --disable-pinch
    --disable-session-crashed-bubble
    --overscroll-history-navigation=0
    --force-device-scale-factor=1
    --autoplay-policy=no-user-gesture-required
    --user-data-dir=/home/archer/.config/archer-chrome
    # The fbdev framebuffer has no real GPU/DRI — letting Chromium try GPU
    # compositing crashes its GPU process and leaves a blank black window.
    # Force software rendering/compositing instead.
    --disable-gpu
    --disable-gpu-compositing
    --use-gl=swiftshader
)

# Try launching the dashboard a few times. Each attempt is bounded by
# `timeout` — if Chromium launches but its renderer hangs without ever
# painting (a blank black window that never exits), waiting on it directly
# would block forever and we'd never reach the on-screen fallback below.
for attempt in 1 2 3; do
    echo "=== launch attempt $attempt: $(date) ===" >> "$LOG"
    timeout 35 /usr/bin/chromium "${CHROME_FLAGS[@]}" --app="$URL" >>"$LOG" 2>&1
    echo "--- chromium exited with code $? (124 = hung / timed out) ---" >> "$LOG"
    sleep 2
done

# All attempts failed — render the captured logs directly in a Chromium
# window so the failure is visible on the monitor without a VT switch.
ERR_HTML=/tmp/archer-kiosk-error.html
{
    echo "<html><body style='background:#000;color:#3f3;font:16px monospace;white-space:pre-wrap;padding:24px'>"
    echo "ARCHER KIOSK — Chromium failed to load the dashboard after 3 attempts.<br><br>"
    echo "--- /tmp/archer-x.log ---<br>"
    sed 's/&/\&amp;/g;s/</\&lt;/g' /tmp/archer-x.log 2>/dev/null
    echo "<br><br>--- $LOG ---<br>"
    sed 's/&/\&amp;/g;s/</\&lt;/g' "$LOG" 2>/dev/null
    echo "</body></html>"
} > "$ERR_HTML"
timeout 60 /usr/bin/chromium "${CHROME_FLAGS[@]}" --app="file://$ERR_HTML" >>"$LOG" 2>&1
echo "--- error-page chromium exited with code $? — handing back to shell ---" >> "$LOG"
KIOSK
chmod +x "$MOUNT/opt/archer/kiosk.sh"

# .bash_profile — on tty1 (physical display), start X kiosk automatically
cat > "$MOUNT/home/archer/.bash_profile" <<'BASHPROFILE'
# tty1 = kiosk display (dashboard). tty2 = maintenance shell (Ctrl+Alt+F2).
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$DISPLAY" ]; then
    # Don't exec — keep bash alive so if X exits we drop to a shell instead
    # of dying and triggering an infinite getty restart loop.
    startx /opt/archer/kiosk.sh -- :0 vt1 >/tmp/archer-x.log 2>&1
    # If X crashed mid-startup it can leave the console stuck in graphics
    # mode (KD_GRAPHICS) — these messages would be invisible otherwise.
    sudo /usr/bin/chvt 1 2>/dev/null
    printf '\033c'
    echo "[archer] X/kiosk exited. See /tmp/archer-x.log and /tmp/archer-chromium.log"
    echo "[archer] Switch to maintenance shell: Ctrl+Alt+F2"
fi
BASHPROFILE
chroot "$MOUNT" chown archer:archer /home/archer/.bash_profile

# Pre-create Xorg directories with correct ownership so X can write its log.
# Without these, Xorg fails immediately before even loading any driver.
chroot "$MOUNT" bash -c "
    mkdir -p /home/archer/.local/share/xorg
    touch /home/archer/.Xauthority
    chown -R archer:archer /home/archer/.local
    chown archer:archer /home/archer/.Xauthority
    chmod 600 /home/archer/.Xauthority
"

# .xinitrc fallback (used if startx is called without an argument)
cat > "$MOUNT/home/archer/.xinitrc" <<'XINITRC'
exec /opt/archer/kiosk.sh
XINITRC
chroot "$MOUNT" chown archer:archer /home/archer/.xinitrc

# Tell NetworkManager to leave wired ethernet alone.
# archer_init brings up ethernet directly with dhclient (no D-Bus dependency).
# NM still handles WiFi and USB tethering.
mkdir -p "$MOUNT/etc/NetworkManager/conf.d"
cat > "$MOUNT/etc/NetworkManager/conf.d/01-unmanaged-ethernet.conf" <<EOF
[keyfile]
unmanaged-devices=type:ethernet
EOF

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

# Leave a fallback resolv.conf — dhclient will overwrite it with DHCP-provided DNS at boot.
# Without this, DNS fails on first boot because NM doesn't manage ethernet.
cat > "$MOUNT/etc/resolv.conf" <<EOF
nameserver 8.8.8.8
nameserver 1.1.1.1
EOF
chroot "$MOUNT" chown -R archer:archer /opt/archer

# Embed OBD2 auth key if one has been generated (see archer-os/obd-auth/keygen.sh).
# The key is in .gitignore and must be generated separately and kept secret.
KEY_SRC="$(dirname "$0")/obd-auth/obd_auth.key"
if [ -f "$KEY_SRC" ]; then
    mkdir -p "$MOUNT/etc/archer"
    chmod 700 "$MOUNT/etc/archer"
    cp "$KEY_SRC" "$MOUNT/etc/archer/obd_auth.key"
    chmod 600 "$MOUNT/etc/archer/obd_auth.key"
    log "OBD2 auth key installed"
else
    log "No OBD2 auth key found — run archer-os/obd-auth/keygen.sh to generate one"
fi

# Embed API key config if it exists — contains GEMINI_API_KEY etc.
# Format: KEY=value, one per line. Never committed to git (.gitignore protected).
# Create: archer-os/archer.env  with  GEMINI_API_KEY=your_key_here
ENV_SRC="$(dirname "$0")/archer.env"
if [ -f "$ENV_SRC" ]; then
    mkdir -p "$MOUNT/etc/archer"
    chmod 700 "$MOUNT/etc/archer"
    cp "$ENV_SRC" "$MOUNT/etc/archer/archer.env"
    chmod 600 "$MOUNT/etc/archer/archer.env"
    log "API key config installed (/etc/archer/archer.env)"
else
    log "No archer.env found — create archer-os/archer.env with GEMINI_API_KEY=... to embed AI key"
fi

step "Compiling archer_init (custom PID 1 — replaces systemd)..."
gcc -static -Os -Wall -std=c11 -D_GNU_SOURCE \
    -o "$MOUNT/sbin/archer_init" \
    "$(dirname "$0")/init/archer_init.c"
chmod 755 "$MOUNT/sbin/archer_init"
log "archer_init installed ($(stat -c%s "$MOUNT/sbin/archer_init") bytes)"

step "Building Archer custom kernel (universal drivers, debug stripped)..."
chmod +x "$(dirname "$0")/kernel/build-kernel.sh"
bash "$(dirname "$0")/kernel/build-kernel.sh" "$MOUNT"
# Capture the kernel version that was built
ARCHER_KERNEL_VER=$(ls "$MOUNT/lib/modules/" | grep '\-archer$' | tail -1)
log "Custom kernel: ${ARCHER_KERNEL_VER}"

step "Generating initramfs with dracut (universal hardware support)..."
# Install dracut inside the image
cp /etc/resolv.conf "$MOUNT/etc/resolv.conf"
DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq dracut
rm -f "$MOUNT/etc/resolv.conf"

# Generic mode: packs all common hardware modules — boots on any machine.
# udev fires at boot, detects hardware, loads only the matching modules.
# Output named initrd.img-VERSION — Debian's update-grub expects this exact pattern.
chroot "$MOUNT" dracut \
    --force \
    --no-hostonly \
    --add "base rootfs-block shutdown" \
    "/boot/initrd.img-${ARCHER_KERNEL_VER}" \
    "$ARCHER_KERNEL_VER" \
    2>&1 | tail -3

INITRD_SIZE=$(( $(stat -c%s "$MOUNT/boot/initrd.img-${ARCHER_KERNEL_VER}") / 1024 / 1024 ))
log "initrd.img-${ARCHER_KERNEL_VER} (${INITRD_SIZE} MB)"

step "Installing Archer systemd service and enabling services..."
cp "$(dirname "$0")/overlay/etc/systemd/system/archer.service" \
    "$MOUNT/etc/systemd/system/archer.service"
chroot "$MOUNT" systemctl enable archer
chroot "$MOUNT" systemctl enable NetworkManager
chroot "$MOUNT" systemctl enable avahi-daemon

step "Installing GRUB bootloader (UEFI + Legacy BIOS)..."
# DEBUG: verbose kernel logging (was "quiet loglevel=0") -- the boot was
# going blank ~28s after "Booting the kernel..." with zero clues why, because
# loglevel=0 silences EVERYTHING including panics/hangs. Once boot is
# confirmed reaching the kiosk reliably, switch this back to "quiet".
cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="loglevel=7 ignore_loglevel init=/sbin/archer_init"
GRUB_CMDLINE_LINUX=""
EOF

chroot "$MOUNT" grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi --bootloader-id=ARCHER \
    --removable --no-nvram >/dev/null 2>&1
chroot "$MOUNT" grub-install --target=i386-pc "$LOOP" >/dev/null 2>&1
chroot "$MOUNT" update-grub >/dev/null 2>&1
log "GRUB configured — default kernel: ${ARCHER_KERNEL_VER}"

cat > "$MOUNT/etc/motd" <<'EOF'

  ╔═══════════════════════════════════╗
  ║       ARCHER TRUCK AI OS          ║
  ╚═══════════════════════════════════╝
  Logs:   cat /run/archer_init.log
  Update: sudo /opt/archer/update.sh
  Status: cat /run/archer_status

EOF

# Update script — works with our custom init (no systemd/systemctl).
# Pulls latest code from git, then kills archer so archer_init restarts it.
cat > "$MOUNT/opt/archer/update.sh" <<'UPDATESCRIPT'
#!/bin/bash
set -e
echo "[archer-update] Pulling latest code..."
cd /opt/archer
git fetch origin claude/archer-truck-ai-system-TlfGE
git reset --hard origin/claude/archer-truck-ai-system-TlfGE

echo "[archer-update] Restarting Archer (archer_init will auto-respawn)..."
ARCHER_PID=$(grep -oP '(?<=archer_pid=)\d+' /run/archer_status 2>/dev/null || true)
if [ -n "$ARCHER_PID" ] && [ "$ARCHER_PID" -gt 0 ] 2>/dev/null; then
    kill "$ARCHER_PID" && echo "[archer-update] Sent SIGTERM to Archer (pid $ARCHER_PID)"
else
    pkill -f "archer.py" 2>/dev/null && echo "[archer-update] Killed archer.py" || true
fi

sleep 2
echo "[archer-update] Done. Archer restarting in background."
echo "[archer-update] Check: curl -s http://127.0.0.1:5000/ | head -1"
UPDATESCRIPT
chmod +x "$MOUNT/opt/archer/update.sh"
chroot "$MOUNT" chown archer:archer /opt/archer/update.sh

step "Unmounting and converting VM image..."
# Tear down bind mounts before unmounting the image filesystem
umount "$MOUNT/dev/pts" 2>/dev/null || true
umount "$MOUNT/dev"     2>/dev/null || true
umount "$MOUNT/sys"     2>/dev/null || true
umount "$MOUNT/proc"    2>/dev/null || true
umount "$MOUNT/boot/efi"
umount "$MOUNT"
losetup -d "$LOOP"
rm -rf "$WORK" 2>/dev/null || true   # WSL2 may block removal of module files — non-fatal

# Convert raw image to VMDK for VMware Workstation Pro
if command -v qemu-img &>/dev/null; then
    log "Converting to VMDK for VMware Workstation Pro..."
    qemu-img convert -f raw -O vmdk -o subformat=streamOptimized "$IMG" "${IMG%.img}.vmdk"
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
