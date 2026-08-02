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
STEP_TOTAL=12

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
    network-manager avahi-daemon dbus openssh-server isc-dhcp-client sudo

# X11 kiosk — pre-register Xorg permissions so the setuid-registration step
# doesn't abort the build in restricted build environments (WSL2 etc.).
# Tell dpkg not to set setuid on Xorg (0755 instead of 4755) — archer user
# has NOPASSWD sudo below so X starts via that instead.
mkdir -p "$MOUNT/var/lib/dpkg"
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

# Tell NetworkManager to leave wired ethernet alone.
# archer_init brings up ethernet directly with dhclient (no D-Bus dependency).
# NM still handles WiFi and USB tethering.
mkdir -p "$MOUNT/etc/NetworkManager/conf.d"
cat > "$MOUNT/etc/NetworkManager/conf.d/01-unmanaged-ethernet.conf" <<EOF
[keyfile]
unmanaged-devices=type:ethernet
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
# sudo: NOPASSWD below lets the non-setuid Xorg above start via sudo, and
# kiosk.sh's error-page fallback uses sudo for dmesg/chvt. input: needed for
# X to read input devices without a setuid Xorg.
chroot "$MOUNT" usermod -aG audio,video,dialout,sudo,input archer

# Give archer passwordless sudo — required by the kiosk pipeline above, not
# general console convenience (deliberately NOT setting a root password the
# way build-vm.sh does for VM debugging; a predictable, source-committed
# root password is a real regression to ship on production truck hardware,
# and nothing the kiosk needs actually depends on one).
mkdir -p "$MOUNT/etc/sudoers.d"
echo "archer ALL=(ALL) NOPASSWD:ALL" > "$MOUNT/etc/sudoers.d/archer"
chmod 440 "$MOUNT/etc/sudoers.d/archer"

mkdir -p "$MOUNT/opt/archer"
if [ -n "$ARCHER_LOCAL_SRC" ] && [ -d "$ARCHER_LOCAL_SRC/.git" ]; then
    log "Using git archive from: $ARCHER_LOCAL_SRC"
    git -C "$ARCHER_LOCAL_SRC" archive HEAD | tar -x -C "$MOUNT/opt/archer"
else
    git clone --branch "$ARCHER_BRANCH" --depth 1 \
        "$ARCHER_REPO" "$MOUNT/opt/archer"
fi

cp /etc/resolv.conf "$MOUNT/etc/resolv.conf"

chroot "$MOUNT" python3 -m venv /opt/archer/.venv
chroot "$MOUNT" /opt/archer/.venv/bin/pip install -q \
    flask edge-tts SpeechRecognition requests pyserial

# Leave a fallback resolv.conf — dhclient will overwrite it with DHCP-provided
# DNS at boot. Without this, DNS fails on first boot because NM doesn't
# manage ethernet (see the unmanaged-ethernet config above).
cat > "$MOUNT/etc/resolv.conf" <<EOF
nameserver 8.8.8.8
nameserver 1.1.1.1
EOF
chroot "$MOUNT" chown -R archer:archer /opt/archer

# archer_init.c runs the OBD2 auth handshake as root (it needs to read the
# root-only /etc/archer/obd_auth.key) and, before doing so, refuses to exec
# anything that isn't root-owned and non-group/world-writable — a fail-closed
# check added after a prior security fix (see git history). The chown -R
# above just re-owned this script to 'archer' along with the rest of
# /opt/archer, which would make that check ALWAYS fail and silently disable
# OBD2 auth on every boot. Re-root it specifically.
#
# NOTE: /opt/archer/.venv/bin/python3 (the interpreter used for both this
# script and archer.py) is NOT re-chowned here on purpose — `python3 -m venv`
# creates it as a SYMLINK to the system python3 outside /opt/archer (e.g.
# /usr/bin/python3), never copied. `chown -R` re-owns the symlink itself but
# does not follow it to re-own the target (verified empirically), and
# archer_init.c's check uses stat(), which DOES follow the symlink — so it
# already sees the untouched, still-root-owned system interpreter and passes
# correctly. If venv creation is ever changed to use --copies, this
# assumption breaks and the interpreter would need the same treatment below.
chroot "$MOUNT" chown root:root /opt/archer/archer-os/obd-auth/obd_auth_client.py
chroot "$MOUNT" chmod 644 /opt/archer/archer-os/obd-auth/obd_auth_client.py

# /etc/archer holds both root-only secrets (obd_auth.key, used by the root-run
# boot-time OBD auth handshake) and archer.py's own HSM master key / env file,
# which archer.py (running as the unprivileged 'archer' user via archer.service)
# must be able to create/read/write at runtime. Group-own it with the sticky
# bit set (like /tmp) so the archer group can create files (needed for hsm.py
# to persist master.key across restarts) without being able to delete or
# overwrite files it doesn't own, such as root's obd_auth.key.
mkdir -p "$MOUNT/etc/archer"
chroot "$MOUNT" chown root:archer /etc/archer
chroot "$MOUNT" chmod 1770 /etc/archer

# ── 7. Kiosk launch pipeline ──────────────────────────────────────
# archer_init.c's tty1 flow is documented as "autologin as archer →
# .bash_profile → startx → kiosk" (see init/archer_init.c) — none of this
# existed here before, so a production image booted to a bare shell on
# tty1 with the Flask backend running invisibly, never showing a dashboard.
#
# CAVEAT not resolved by this step: the fbdev Xorg driver below needs
# CONFIG_FB_VESA/CONFIG_FB_EFI kernel support (see the comment on the driver
# config). This build installs the stock Debian linux-image-amd64 package,
# not the custom-tuned kernel from kernel/archer.config that build-vm.sh
# uses — whether the stock kernel has those options enabled is not verified
# here. If the kiosk pipeline doesn't actually render anything, that's the
# first thing to check, and is very likely why the custom kernel work
# exists in the first place (tracked separately).
step "Setting up kiosk launch pipeline (X11 + Chromium)..."

# Kiosk launch script — waits for Flask, then opens Chromium fullscreen
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
    echo "<html><body style='background:#000;color:#3f3;font:14px monospace;white-space:pre-wrap;padding:24px'>"
    echo "ARCHER KIOSK — Chromium failed to load the dashboard after 3 attempts.<br><br>"
    echo "--- dmesg (full kernel boot log — scrolls too fast to read live, readable here) ---<br>"
    sudo /usr/bin/dmesg 2>/dev/null | sed 's/&/\&amp;/g;s/</\&lt;/g'
    echo "<br><br>--- /run/archer_init.log ---<br>"
    sed 's/&/\&amp;/g;s/</\&lt;/g' /run/archer_init.log 2>/dev/null
    echo "<br><br>--- /tmp/archer-x.log ---<br>"
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

# ── 8. Compile and install Archer custom init (PID 1) ────────────
step "Compiling archer_init (custom PID 1 — replaces systemd)..."
# Compile statically on the build host — no deps needed in the target image
gcc -static -Os -Wall -std=c11 -D_GNU_SOURCE \
    -o "$MOUNT/sbin/archer_init" \
    "$(dirname "$0")/init/archer_init.c"
chmod 755 "$MOUNT/sbin/archer_init"

# Tell GRUB to use our init instead of systemd
# This is set below in the GRUB config step — kept here as a note
log "archer_init installed at /sbin/archer_init ($(stat -c%s "$MOUNT/sbin/archer_init") bytes)"

# ── 9. Archer systemd service ────────────────────────────────────
step "Installing Archer systemd service and enabling services..."
# We still install the systemd service as a fallback (if init= is removed from cmdline)
cp "$(dirname "$0")/overlay/etc/systemd/system/archer.service" \
    "$MOUNT/etc/systemd/system/archer.service"

chroot "$MOUNT" systemctl enable archer
chroot "$MOUNT" systemctl enable NetworkManager
chroot "$MOUNT" systemctl enable avahi-daemon

# Disable unneeded services for speed
chroot "$MOUNT" systemctl disable ssh 2>/dev/null || true

# ── 10. Boot splash + GRUB ──────────────────────────────────────
step "Configuring GRUB bootloader (UEFI + Legacy BIOS)..."
# config/grub.cfg sets a GRUB superuser password on the edit/command-line
# menu (so physical/USB access can't bypass boot via init=/bin/sh) — a real
# grub-mkpasswd-pbkdf2 hash is already in place there, not a placeholder.
# If the password is ever rotated, regenerate with `grub-mkpasswd-pbkdf2`
# and replace the hash in config/grub.cfg before shipping the next image.
#
# Two bugs fixed here, found while starting the custom-kernel work (kept as
# its own pass since both are pre-existing and unrelated to whether that
# work happens at all — see git history):
#
# 1. config/grub.cfg's "Archer OS" entry referenced generic, unsuffixed
#    /boot/vmlinuz and /boot/initrd.img — nothing anywhere in this build
#    pipeline ever creates files with those exact names (every installed
#    kernel, here or from a future custom-kernel build, only ever lands
#    version-suffixed). That entry could never have actually booted.
#    Fixed by substituting the real installed kernel's version into the
#    __ARCHER_KERNEL_VERSION__ placeholder below before installing the file.
#
# 2. GRUB_DEFAULT=0 does not reliably select this entry. grub-mkconfig
#    concatenates /etc/grub.d/ scripts in filename order — 10_linux (which
#    auto-generates an entry for every kernel in /boot) runs before this
#    file (40_archer), so its entry becomes position 0, not this one. Per
#    the GRUB manual (node "Authentication and authorisation"):
#    grub-mkconfig has no built-in authentication support at all, so
#    10_linux's entries can never be marked --unrestricted — meaning once
#    superusers is set (as this file does), EVERY auto-generated entry
#    requires the password just to boot, not just to edit. Verified this
#    ordering directly: installed the exact grub-pc-bin/grub-efi-amd64-bin
#    packages this script uses in a real chroot and ran the actual
#    grub-mkconfig against it. Fixed by giving the entry a stable --id
#    (archer-os, in config/grub.cfg) and setting GRUB_DEFAULT to that id
#    instead of a position.
#
# NOT verified, flagged rather than guessed at (same reason as the fbdev/
# CONFIG_FB_VESA caveat a few steps up — this sandbox can't reach
# deb.debian.org to check, and there's no real hardware to boot-test
# against): whether initramfs-tools (or another initrd generator) actually
# gets installed as a side effect of installing linux-image-amd64 via
# debootstrap here — this script never installs one explicitly, unlike
# build-vm.sh's explicit `apt-get install dracut` + dracut invocation for
# its custom kernel. If /boot/initrd.img-<version> doesn't exist after
# debootstrap, the substitution below will point the Archer OS entry at a
# real but missing file. Worth being one of the first things checked once
# real hardware exists — same as the fbdev question, cheap to answer by
# just booting it and seeing whether the initrd loads, and currently
# unanswerable from here.
ARCHER_KERNEL_VER=$(basename "$(ls "$MOUNT"/boot/vmlinuz-* 2>/dev/null | head -1)" | sed 's/^vmlinuz-//')
if [ -z "$ARCHER_KERNEL_VER" ]; then
    die "No /boot/vmlinuz-* found after debootstrap — cannot configure GRUB to boot it"
fi
log "Stock kernel installed: ${ARCHER_KERNEL_VER}"
sed "s/__ARCHER_KERNEL_VERSION__/${ARCHER_KERNEL_VER}/g" \
    "$(dirname "$0")/config/grub.cfg" > "$MOUNT/etc/grub.d/40_archer"
chmod +x "$MOUNT/etc/grub.d/40_archer"

cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=archer-os
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

# ── 11. MOTD / status screen ────────────────────────────────────
cat > "$MOUNT/etc/motd" <<'EOF'

  ╔═══════════════════════════════════════╗
  ║          ARCHER TRUCK AI OS           ║
  ╚═══════════════════════════════════════╝

  Service:  sudo systemctl status archer
  Logs:     sudo journalctl -u archer -f
  Update:   sudo /opt/archer/usb-os/update.sh

EOF

# ── 11. step() call for the MOTD block above ─────────────────────
step "Writing MOTD and finalizing filesystem..."

# ── 12. Cleanup + unmount ───────────────────────────────────────
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
