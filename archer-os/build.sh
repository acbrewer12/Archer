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
STEP_TOTAL=14

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
# RESOLVED, not just flagged: the fbdev Xorg driver below needs
# CONFIG_FB_VESA/CONFIG_FB_EFI kernel support (see the comment on the driver
# config). At the time this kiosk pipeline first landed, this build only
# ever installed the stock Debian linux-image-amd64 kernel, and whether
# stock Debian has those options enabled was unverified and unverifiable
# from this sandbox. The custom-kernel step further down in this script
# now builds and boots kernel/archer.config instead, which was read
# directly and confirmed to set CONFIG_FB=y, CONFIG_FB_VESA=y,
# CONFIG_FB_EFI=y, CONFIG_FRAMEBUFFER_CONSOLE=y, and CONFIG_VT=y — exactly
# what this driver needs. The stock kernel's own support for these options
# remains genuinely unverified (still can't reach deb.debian.org from
# here), but that no longer matters for whether the kiosk renders, since
# the stock kernel isn't what boots by default anymore — only relevant if
# someone deliberately falls back to testing it later.
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
    #
    # sudo is required here, not optional — found by actually booting the
    # VM variant of this image: Xorg's setuid bit is deliberately stripped
    # above (0755 instead of 4755, see the dpkg-statoverride comment)
    # specifically so it doesn't rely on setuid working, with the archer
    # user's NOPASSWD sudo meant to take its place — but this line called
    # `startx` plain, so Xorg ran as an ordinary unprivileged user and
    # failed immediately with "_XSERVTransmkdir: ERROR: euid != 0"
    # (confirmed via /tmp/archer-x.log on a real boot), cascading into a
    # generic "no screens found" that had nothing to do with the
    # framebuffer itself.
    sudo startx /opt/archer/kiosk.sh -- :0 vt1 >/tmp/archer-x.log 2>&1
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

# ── 9. Custom kernel (CAN bus, 1000Hz timer, USB-serial tuning) ──
# Previously only build-vm.sh built this — production shipped the stock
# Debian kernel instead, with the fbdev/CONFIG_FB_VESA question from the
# kiosk step above left open specifically because of that gap. This is the
# tuning kernel/archer.config exists for in the first place.
#
# Deliberately does NOT remove the stock kernel installed by debootstrap
# above — both coexist in /boot. Three reasons: (1) it's the only way to
# ever actually answer the open fbdev/initrd questions on real hardware —
# removing it would foreclose testing "does the stock kernel work too?"
# permanently, not just defer it; (2) update-grub's auto-generated entries
# for it still exist as a real (if password-gated, per the prior GRUB fix)
# recovery path if the custom kernel fails to boot; (3) build-kernel.sh
# itself never touches or removes it — introducing removal logic here
# would be new, untested surface area on top of an already-higher-risk
# change, for no requirement that asked for it.
step "Building Archer custom kernel (CAN bus, 1000Hz timer, universal drivers)..."
chmod +x "$(dirname "$0")/kernel/build-kernel.sh"
bash "$(dirname "$0")/kernel/build-kernel.sh" "$MOUNT"
# Same detection pattern build-vm.sh already uses — the -archer suffix is
# unique to kernel/build-kernel.sh's own KERNEL_RELEASE="${KERNEL_VERSION}-archer",
# so this can't accidentally match the stock kernel now sitting alongside it.
ARCHER_KERNEL_VER=$(ls "$MOUNT/lib/modules/" 2>/dev/null | grep -- '-archer$' | tail -1)
if [ -z "$ARCHER_KERNEL_VER" ]; then
    die "Custom kernel build did not produce a -archer kernel under /lib/modules — check the [KERNEL] output above"
fi
log "Custom kernel: ${ARCHER_KERNEL_VER}"

step "Generating initramfs for both kernels with dracut..."
cp /etc/resolv.conf "$MOUNT/etc/resolv.conf"
# Debian's dracut package Conflicts: initramfs-tools, and its postinst
# registers a dpkg trigger that unconditionally runs a bare-defaults dracut
# pass (no --no-hostonly, no --add) against EVERY kernel version already
# present under /boot — not just the one this script cares about. Since the
# custom-kernel step above runs first, both the stock and the custom kernel
# are already in /boot by the time this apt-get install runs, so that
# trigger fires for both, silently, as a side effect of installing a
# package. Found by extracting and reading the real dracut .deb's
# postinst/trigger scripts — not documented anywhere GRUB/dracut normally
# advertise.
#
# The explicit --force call below overwrites the custom kernel's trigger-
# generated initrd with a correctly-flagged, verified one either way, so
# that kernel was never actually at risk. The stock kernel's trigger-
# generated initrd was the real gap: no check, no log line, generated with
# whatever narrower/host-specific dracut defaults the trigger uses rather
# than the generic --no-hostonly config this build wants for hardware it
# hasn't tested against — the opposite of "safe recovery fallback." Now
# explicitly regenerated and verified with the same generic flags as the
# custom kernel, immediately below.
DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq dracut
rm -f "$MOUNT/etc/resolv.conf"

# Generic mode: packs all common hardware modules — same reasoning as
# build-vm.sh, this image needs to boot on whatever the actual head-unit
# hardware turns out to be, not one specific tested machine.
chroot "$MOUNT" dracut \
    --force \
    --no-hostonly \
    --add "base rootfs-block shutdown" \
    "/boot/initrd.img-${ARCHER_KERNEL_VER}" \
    "$ARCHER_KERNEL_VER" \
    2>&1 | tail -3
if [ ! -f "$MOUNT/boot/initrd.img-${ARCHER_KERNEL_VER}" ]; then
    die "dracut did not produce /boot/initrd.img-${ARCHER_KERNEL_VER}"
fi
INITRD_SIZE=$(( $(stat -c%s "$MOUNT/boot/initrd.img-${ARCHER_KERNEL_VER}") / 1024 / 1024 ))
log "initrd.img-${ARCHER_KERNEL_VER} (${INITRD_SIZE} MB)"

# Stock kernel (the fallback path — see the custom-kernel step's comment on
# why it's kept). Detected by excluding the -archer suffix, mirroring how
# ARCHER_KERNEL_VER itself is detected above.
STOCK_KERNEL_VER=$(ls "$MOUNT/lib/modules/" 2>/dev/null | grep -v -- '-archer$' | tail -1)
if [ -z "$STOCK_KERNEL_VER" ]; then
    die "No stock kernel found under /lib/modules alongside the custom one — debootstrap's linux-image-amd64 install may have failed silently"
fi
chroot "$MOUNT" dracut \
    --force \
    --no-hostonly \
    --add "base rootfs-block shutdown" \
    "/boot/initrd.img-${STOCK_KERNEL_VER}" \
    "$STOCK_KERNEL_VER" \
    2>&1 | tail -3
if [ ! -f "$MOUNT/boot/initrd.img-${STOCK_KERNEL_VER}" ]; then
    die "dracut did not produce /boot/initrd.img-${STOCK_KERNEL_VER} for the stock fallback kernel"
fi
STOCK_INITRD_SIZE=$(( $(stat -c%s "$MOUNT/boot/initrd.img-${STOCK_KERNEL_VER}") / 1024 / 1024 ))
log "Stock fallback kernel: ${STOCK_KERNEL_VER} — initrd.img-${STOCK_KERNEL_VER} (${STOCK_INITRD_SIZE} MB)"

# ── 10. Archer systemd service ────────────────────────────────────
step "Installing Archer systemd service and enabling services..."
# We still install the systemd service as a fallback (if init= is removed from cmdline)
cp "$(dirname "$0")/overlay/etc/systemd/system/archer.service" \
    "$MOUNT/etc/systemd/system/archer.service"

chroot "$MOUNT" systemctl enable archer
chroot "$MOUNT" systemctl enable NetworkManager
chroot "$MOUNT" systemctl enable avahi-daemon

# Disable unneeded services for speed
chroot "$MOUNT" systemctl disable ssh 2>/dev/null || true

# ── 11. Boot splash + GRUB ──────────────────────────────────────
step "Configuring GRUB bootloader (UEFI + Legacy BIOS)..."
# config/grub.cfg sets a GRUB superuser password on the edit/command-line
# menu (so physical/USB access can't bypass boot via init=/bin/sh) — a real
# grub-mkpasswd-pbkdf2 hash is already in place there, not a placeholder.
# If the password is ever rotated, regenerate with `grub-mkpasswd-pbkdf2`
# and replace the hash in config/grub.cfg before shipping the next image.
#
# Two bugs previously fixed here (pre-existing, unrelated to whether the
# custom-kernel step above happens at all — see git history):
#
# 1. config/grub.cfg's "Archer OS" entry referenced generic, unsuffixed
#    /boot/vmlinuz and /boot/initrd.img — nothing anywhere in this build
#    pipeline ever creates files with those exact names (every installed
#    kernel only ever lands version-suffixed). That entry could never have
#    actually booted. Fixed by substituting the real kernel version into
#    the __ARCHER_KERNEL_VERSION__ placeholder below before installing the
#    file — ARCHER_KERNEL_VER is set above by the custom-kernel step, and
#    reused here rather than re-detected, on purpose: re-detecting from
#    /boot/vmlinuz-* here would now be ambiguous, since the stock kernel
#    from debootstrap and the custom -archer kernel both sit in /boot side
#    by side (see the custom-kernel step's comment for why the stock one
#    is deliberately kept rather than removed).
#
# 2. GRUB_DEFAULT=0 does not reliably select this entry. grub-mkconfig
#    concatenates /etc/grub.d/ scripts in filename order — 10_linux (which
#    auto-generates an entry for every kernel in /boot, now two of them)
#    runs before this file (40_archer), so its entry becomes position 0,
#    not this one. Per the GRUB manual (node "Authentication and
#    authorisation"): grub-mkconfig has no built-in authentication support
#    at all, so 10_linux's entries can never be marked --unrestricted —
#    meaning once superusers is set, EVERY auto-generated entry requires
#    the password just to boot, not just to edit. Verified this ordering
#    directly: installed the exact grub-pc-bin/grub-efi-amd64-bin packages
#    this script uses in a real chroot and ran the actual grub-mkconfig
#    against it. Fixed by giving the entry a stable --id (archer-os, in
#    config/grub.cfg) and setting GRUB_DEFAULT to that id instead of a
#    position.
#
# RESOLVED, not just flagged: whether the stock kernel has a working initrd
# for its own fallback 10_linux entry to actually boot with. It doesn't get
# one from debootstrap alone (no initramfs generator installed explicitly),
# but the custom-kernel step's dracut pass now explicitly generates and
# verifies one for it too — not relying on dracut's own postinst trigger,
# which (per that step's comment) fires for every kernel in /boot as a side
# effect but with different, less portable defaults and no verification of
# its own. Both kernels now have a checked, consistently-configured initrd.
log "GRUB will boot: ${ARCHER_KERNEL_VER} (stock kernel kept in /boot as a verified fallback, not GRUB's default)"
sed "s/__ARCHER_KERNEL_VERSION__/${ARCHER_KERNEL_VER}/g" \
    "$(dirname "$0")/config/grub.cfg" > "$MOUNT/etc/grub.d/40_archer"
chmod +x "$MOUNT/etc/grub.d/40_archer"

# vga=791 (1024x768, 16-bit) — found missing by actually booting the VM
# variant of this image: CONFIG_FB_VESA=y being compiled in (see the
# "RESOLVED" comment above the kiosk pipeline) only means the driver exists,
# not that it activates. Under legacy BIOS boot, vesafb needs an explicit
# vga= mode number or it never creates /dev/fb0 at all, which made Xorg's
# fbdev driver fail with "no screens found" — confirmed via /proc/cmdline
# and dmesg on a real boot, not assumed. CONFIG_FB_EFI (UEFI boot) doesn't
# need this — vga= is simply unused/harmless on that path.
cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=archer-os
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=0 vga=791 init=/sbin/archer_init"
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

# ── 12. MOTD / status screen ────────────────────────────────────
cat > "$MOUNT/etc/motd" <<'EOF'

  ╔═══════════════════════════════════════╗
  ║          ARCHER TRUCK AI OS           ║
  ╚═══════════════════════════════════════╝

  Service:  sudo systemctl status archer
  Logs:     sudo journalctl -u archer -f
  Update:   sudo /opt/archer/usb-os/update.sh

EOF

step "Writing MOTD and finalizing filesystem..."

# ── 13. Cleanup + unmount ───────────────────────────────────────
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
