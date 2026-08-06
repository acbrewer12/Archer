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
# Tell dpkg not to set setuid on Xorg (0755 instead of 4755). /usr/bin/Xorg
# is only a dispatcher script; the real privilege comes from the setuid
# /usr/lib/xorg/Xorg.wrap that xserver-xorg-legacy installs below, which is
# what lets .bash_profile start the session unprivileged.
mkdir -p "$MOUNT/var/lib/dpkg"
chroot "$MOUNT" bash -c "dpkg-statoverride --add root root 0755 /usr/bin/Xorg 2>/dev/null; true"
echo "force-unsafe-io" > "$MOUNT/etc/dpkg/dpkg.cfg.d/99archer-build"
# xserver-xorg-legacy is required, not optional — found by actually booting
# the VM variant of this image: without it, /usr/lib/xorg/Xorg.wrap doesn't
# exist, so /usr/bin/Xorg's own dispatcher script falls through to exec'ing
# the real Xorg binary directly with none of Xwrapper.config's settings
# (below) applied. Plain `startx` then runs Xorg as whatever user invoked
# it, failing immediately with "_XSERVTransmkdir: ERROR: euid != 0". With
# this package present, Xorg.wrap elevates only the X server, so
# .bash_profile can run the whole desktop session as the archer user.
DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq \
    --no-install-recommends \
    xorg xinit xserver-xorg-legacy chromium x11-xserver-utils fontconfig \
    openbox tint2 pcmanfm lxterminal desktop-file-utils 2>&1 || \
    log "WARNING: X11/Chromium install had errors (kiosk may not work)"
rm -f "$MOUNT/etc/dpkg/dpkg.cfg.d/99archer-build"

# Archer wordmark font (Orbitron) and the sign-in/technical-readout font
# (Share Tech Mono), both real Google Fonts (SIL Open Font License) —
# fetched directly rather than via fonts.googleapis.com (that CSS-based
# @font-face path only works for pages Chromium renders; native desktop
# chrome like openbox's window titles and tint2's taskbar need the actual
# .ttf files installed as system fonts to reference them at all). Pulled
# from Google's own font repo mirror on GitHub rather than fonts.google.com
# directly, since that's what this build environment can actually reach.
mkdir -p "$MOUNT/usr/share/fonts/truetype/archer"
curl -fsSL "https://raw.githubusercontent.com/google/fonts/main/ofl/orbitron/Orbitron%5Bwght%5D.ttf" \
    -o "$MOUNT/usr/share/fonts/truetype/archer/Orbitron.ttf" || \
    log "WARNING: Orbitron font download failed — desktop chrome will fall back to a default font"
curl -fsSL "https://raw.githubusercontent.com/google/fonts/main/ofl/sharetechmono/ShareTechMono-Regular.ttf" \
    -o "$MOUNT/usr/share/fonts/truetype/archer/ShareTechMono-Regular.ttf" || \
    log "WARNING: Share Tech Mono font download failed — desktop chrome will fall back to a default font"
chroot "$MOUNT" fc-cache -f >/dev/null 2>&1 || true
# Allow non-root users to start X
mkdir -p "$MOUNT/etc/X11"
cat > "$MOUNT/etc/X11/Xwrapper.config" <<'XWRAP'
allowed_users=anybody
needs_root_rights=yes
XWRAP

# modesetting driver, not fbdev — found by actually booting the VM variant
# of this image: even with a genuinely valid kernel framebuffer active
# (confirmed via dmesg), Xorg's old xf86-video-fbdev driver still failed
# with "no screens found" against it — a real fbdev-driver/efifb
# compatibility gap, not something fixable from this config file
# (permissions, PCI/GPU auto-bind matching, and the fbdev device path were
# all ruled out first). modesetting is Xorg's modern, generally more
# robust driver, and works against the CONFIG_DRM_SIMPLEDRM=y device (see
# kernel/archer.config and the video=efifb:off/video=vesafb:off GRUB
# parameters below) instead of the kernel's raw /dev/fb0.
mkdir -p "$MOUNT/etc/X11/xorg.conf.d"
cat > "$MOUNT/etc/X11/xorg.conf.d/10-modesetting.conf" <<'XORGCONF'
Section "Device"
    Identifier  "Archer Display"
    Driver      "modesetting"
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

# Desktop session — real openbox window manager + tint2 taskbar +
# pcmanfm/lxterminal, replacing the old single-app --kiosk Chromium
# lock-in. kiosk.sh keeps its name (referenced by .bash_profile/.xinitrc
# below) but is now the desktop session launcher, not the app launcher —
# it waits for Flask, then hands off to openbox; openbox itself sources
# ~/.config/openbox/autostart, which launches tint2 and the dashboard.
cat > "$MOUNT/opt/archer/kiosk.sh" <<'KIOSK'
#!/bin/bash
# Create Xorg / desktop config directories — root is now rw thanks to
# archer_init's remount. A missing/unwritable profile dir can make
# Chromium hang silently on a fresh boot instead of erroring out.
mkdir -p /home/archer/.local/share/xorg      2>/dev/null || true
mkdir -p /home/archer/.config/archer-chrome  2>/dev/null || true
mkdir -p /home/archer/.config/openbox        2>/dev/null || true
mkdir -p /home/archer/.config/tint2          2>/dev/null || true
touch /home/archer/.Xauthority 2>/dev/null || true

# The Flask wait deliberately does NOT live here any more — it moved into
# desktop-dashboard.sh. Gating the whole session on the backend meant the
# desktop itself was held hostage to archer.py's startup, and worse, the
# wait gave up after 45s and started the desktop anyway. On USB-booted real
# hardware archer.py takes longer than that to answer, so Chromium launched
# against a backend that was not up yet, got a connection error, and never
# reloads on its own — a permanently white "dashboard" even once Flask
# finished starting. Now the desktop appears straight away (taskbar and
# terminal usable while the backend boots) and only the dashboard waits.
# Disable screensaver / power management
xset s off -dpms 2>/dev/null || true

# The session normally runs as the archer user now (see .bash_profile),
# so HOME is already /home/archer. This export is kept because
# .bash_profile still has a sudo fallback path, and under `sudo startx`
# HOME becomes /root — verified empirically with a real NOPASSWD-sudo
# test user, not assumed (sudo's env_reset resets HOME to the target
# user's home). openbox and tint2 both discover their config via
# $HOME/.config, so on that path they would read /root/.config/... , find
# nothing, and silently fall back to stock defaults: no taskbar, no
# dashboard, no theme, generic Debian root menu. Pin it either way.
export HOME=/home/archer
export XDG_CONFIG_HOME=/home/archer/.config

# Solid black before anything else draws — no flash of X's default gray
# root window while openbox/tint2/chromium are still starting up.
xsetroot -solid "#050508" 2>/dev/null || true

# openbox blocks here for the life of the session — this is startx's
# client (see .bash_profile).
#
# --startup is REQUIRED, not optional: the bare `openbox` binary does NOT
# run ~/.config/openbox/autostart by itself. Only openbox-session does,
# and it does so precisely by passing --startup — its own source reads
#   exec /usr/bin/openbox --startup ".../openbox-autostart OPENBOX"
# under the comment "Run Openbox, and have it run the autostart stuff".
# Without this, tint2 and the dashboard never launch at all and the
# screen is an empty desktop. We invoke our own autostart directly
# rather than the distro's openbox-autostart helper, which hardcodes an
# arch-specific path and ends by exec'ing a Python XDG-autostart script
# that needs PyXDG (not installed here, --no-install-recommends).
# --config-file is belt-and-braces alongside the HOME export above.
exec openbox \
    --config-file /home/archer/.config/openbox/rc.xml \
    --startup "sh /home/archer/.config/openbox/autostart"
KIOSK
chmod +x "$MOUNT/opt/archer/kiosk.sh"

# Dashboard launcher — run from openbox's autostart, the root-menu
# "Dashboard" item, and the Ctrl+Alt+D keybind (see rc.xml below).
# Chromium does NOT reuse its window on repeat launches against the same
# --user-data-dir (its process singleton hands the command line to the
# running instance, which for --app= opens a NEW app window) — so the
# script itself guards against that with a pgrep check.
cat > "$MOUNT/opt/archer/desktop-dashboard.sh" <<'DASHSCRIPT'
#!/bin/bash
mkdir -p /home/archer/.config/archer-chrome 2>/dev/null || true

URL="http://127.0.0.1:5000/dashboard"
LOG=/tmp/archer-chromium.log

# Chromium's process singleton does NOT raise/focus an existing window when
# re-invoked — for --app= it opens ANOTHER app window. So the menu item and
# Ctrl+Alt+D would each stack up a duplicate dashboard (and a duplicate
# taskbar button) every time they're used, which is exactly what someone
# will do repeatedly when the screen looks wrong. Bail out if it's already
# running; the existing window is reachable via the taskbar or Alt+Tab.
if pgrep -f -- '--user-data-dir=/home/archer/.config/archer-chrome' >/dev/null 2>&1; then
    exit 0
fi

# Deliberately NOT truncating $LOG here: a still-running Chromium holds it
# open in append mode, so truncating would blow away the very output needed
# to diagnose why the first window misbehaved.

# Wait for the backend before launching, because Chromium does not retry a
# failed load. 180s rather than the old 45s: that figure was tuned against a
# VM disk, and a USB-booted head unit is far slower to bring archer.py up
# (venv interpreter plus a large import graph off slow flash). Exceeding the
# old timeout is exactly what produced a white connection-error page that
# never recovered even after Flask came up.
FLASK_UP=0
for i in $(seq 1 180); do
    { exec 3<>/dev/tcp/127.0.0.1/5000; } 2>/dev/null && { exec 3<&- 3>&-; FLASK_UP=1; break; }
    sleep 1
done

if [ "$FLASK_UP" != "1" ]; then
    # Never hand the user a blank white browser error — say what happened
    # and show the evidence, on-screen, without needing a VT switch.
    echo "=== backend never answered on 127.0.0.1:5000 within 180s ===" >> "$LOG"
    ERR_HTML=/tmp/archer-dashboard-error.html
    _esc() { sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
    {
        echo "<html><head><meta charset='utf-8'><title>Archer</title></head>"
        echo "<body style='background:#050508;color:#dde4e8;font:13px monospace;padding:28px'>"
        echo "<div style='color:#00e5ff;font-size:26px;letter-spacing:6px'>ARCHER</div>"
        echo "<p style='color:#ff3333'>The Archer backend did not answer on 127.0.0.1:5000 within 180 seconds.</p>"
        echo "<p style='color:#6a7a88'>The desktop is working — this is the backend, not the display."
        echo "Press Ctrl+Alt+T for a terminal. Press Ctrl+R in this window to retry once the"
        echo "backend is up — Ctrl+Alt+D will not help here, the already-running-window guard"
        echo "in desktop-dashboard.sh treats this window as the dashboard and exits.</p>"
        echo "<pre style='color:#6a7a88'>--- is archer.py running? ---</pre><pre>"
        ps -eo pid,user,args 2>/dev/null | grep -F archer.py | grep -v grep | _esc
        echo "</pre><pre style='color:#6a7a88'>--- /run/archer_init.log (tail) ---</pre><pre>"
        tail -n 40 /run/archer_init.log 2>/dev/null | _esc
        echo "</pre><pre style='color:#6a7a88'>--- listening sockets ---</pre><pre>"
        (ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null) | _esc
        echo "</pre></body></html>"
    } > "$ERR_HTML"
    URL="file://$ERR_HTML"
fi

CHROME_FLAGS=(
    --app="$URL"
    --start-maximized
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
    # simpledrm (see kernel/archer.config) provides mode-setting only, not
    # real GPU/DRI acceleration — letting Chromium try GPU compositing
    # crashes its GPU process and leaves a blank black window. Force
    # software rendering/compositing instead.
    --disable-gpu
    --disable-gpu-compositing
    --use-gl=swiftshader
)
# Chromium's namespace sandbox needs unprivileged user namespaces
# (CONFIG_USER_NS — now enabled in kernel/archer.config). Rather than
# hardcoding --no-sandbox, which switches off a real security boundary AND
# makes Chromium paint a permanent "You are using an unsupported
# command-line flag: --no-sandbox" banner across the top of the dashboard,
# actually test the capability and only fall back when it genuinely is not
# there. On a kernel with user namespaces this runs sandboxed and the banner
# is gone; on one without, the flag is added automatically so the dashboard
# still starts rather than failing outright. unshare is util-linux, part of
# the debootstrap base, so it is always present.
if ! unshare --user --map-root-user true 2>/dev/null; then
    echo "=== user namespaces unavailable — falling back to --no-sandbox ===" >> "$LOG"
    CHROME_FLAGS+=(--no-sandbox)
fi

echo "=== launch: $(date) ===" >> "$LOG"
exec /usr/bin/chromium "${CHROME_FLAGS[@]}" >>"$LOG" 2>&1
DASHSCRIPT
chmod +x "$MOUNT/opt/archer/desktop-dashboard.sh"

# openbox autostart — sourced automatically by openbox on session start.
mkdir -p "$MOUNT/home/archer/.config/openbox"
cat > "$MOUNT/home/archer/.config/openbox/autostart" <<'AUTOSTART'
# Explicit -c path rather than relying on $HOME discovery — this whole
# session runs as root via `sudo startx` (see kiosk.sh), so belt-and-braces
# alongside the HOME export there.
tint2 -c /home/archer/.config/tint2/tint2rc &
sleep 1
/opt/archer/desktop-dashboard.sh &
AUTOSTART
chmod +x "$MOUNT/home/archer/.config/openbox/autostart"

# openbox main config — every element name and section ordering checked
# directly against openbox's real rc.xsd schema (not assumed), including
# which sections require strict element order (xsd:sequence) vs allow any
# order (xsd:all). Onyx is a long-stable, bundled-by-default dark theme
# for window decorations; Orbitron/Share Tech Mono match the dashboard's
# own wordmark/technical-readout fonts (see kernel/archer.config's font
# install step above) so the native desktop chrome and the
# Chromium-rendered dashboard read as the same product.
cat > "$MOUNT/home/archer/.config/openbox/rc.xml" <<'RCXML'
<?xml version="1.0" encoding="UTF-8"?>

<openbox_config xmlns="http://openbox.org/3.4/rc"
		xmlns:xi="http://www.w3.org/2001/XInclude">

<resistance>
  <strength>10</strength>
  <screen_edge_strength>20</screen_edge_strength>
</resistance>

<focus>
  <focusNew>yes</focusNew>
  <followMouse>no</followMouse>
  <focusLast>yes</focusLast>
  <underMouse>no</underMouse>
  <focusDelay>200</focusDelay>
  <raiseOnFocus>no</raiseOnFocus>
</focus>

<placement>
  <policy>Smart</policy>
  <center>yes</center>
  <monitor>Primary</monitor>
  <primaryMonitor>1</primaryMonitor>
</placement>

<theme>
  <name>Archer</name>
  <titleLayout>NLIMC</titleLayout>
  <keepBorder>yes</keepBorder>
  <animateIconify>yes</animateIconify>
  <font place="ActiveWindow">
    <name>Orbitron</name>
    <size>9</size>
    <weight>bold</weight>
    <slant>normal</slant>
  </font>
  <font place="InactiveWindow">
    <name>Orbitron</name>
    <size>9</size>
    <weight>bold</weight>
    <slant>normal</slant>
  </font>
  <font place="MenuHeader">
    <name>Orbitron</name>
    <size>9</size>
    <weight>bold</weight>
    <slant>normal</slant>
  </font>
  <font place="MenuItem">
    <name>Share Tech Mono</name>
    <size>10</size>
    <weight>normal</weight>
    <slant>normal</slant>
  </font>
  <font place="ActiveOnScreenDisplay">
    <name>Share Tech Mono</name>
    <size>9</size>
    <weight>bold</weight>
    <slant>normal</slant>
  </font>
  <font place="InactiveOnScreenDisplay">
    <name>Share Tech Mono</name>
    <size>9</size>
    <weight>normal</weight>
    <slant>normal</slant>
  </font>
</theme>

<desktops>
  <number>1</number>
  <firstdesk>1</firstdesk>
  <names>
    <name>Dashboard</name>
  </names>
</desktops>

<resize>
  <drawContents>yes</drawContents>
  <popupShow>Nonpixel</popupShow>
  <popupPosition>Center</popupPosition>
</resize>

<margins>
  <top>0</top>
  <bottom>0</bottom>
  <left>0</left>
  <right>0</right>
</margins>

<dock>
  <position>TopLeft</position>
  <floatingX>0</floatingX>
  <floatingY>0</floatingY>
  <noStrut>no</noStrut>
  <stacking>Above</stacking>
  <direction>Vertical</direction>
  <autoHide>no</autoHide>
  <hideDelay>300</hideDelay>
  <showDelay>300</showDelay>
  <moveButton>Middle</moveButton>
</dock>

<keyboard>
  <chainQuitKey>C-g</chainQuitKey>

  <keybind key="A-Tab">
    <action name="NextWindow"/>
  </keybind>
  <keybind key="A-S-Tab">
    <action name="PreviousWindow"/>
  </keybind>
  <keybind key="A-F4">
    <action name="Close"/>
  </keybind>
  <keybind key="A-F9">
    <action name="Iconify"/>
  </keybind>
  <keybind key="A-F10">
    <action name="ToggleMaximizeFull"/>
  </keybind>

  <keybind key="C-A-t">
    <action name="Execute">
      <command>lxterminal</command>
    </action>
  </keybind>
  <keybind key="C-A-d">
    <action name="Execute">
      <command>/opt/archer/desktop-dashboard.sh</command>
    </action>
  </keybind>
  <keybind key="C-A-f">
    <action name="Execute">
      <command>pcmanfm</command>
    </action>
  </keybind>
  <!-- Per-window menu (Move/Resize/layer/Send to/Close) — otherwise
       unreachable from the keyboard entirely. -->
  <keybind key="A-space">
    <action name="ShowMenu"><menu>client-menu</menu></action>
  </keybind>
</keyboard>

<mouse>
  <dragThreshold>8</dragThreshold>
  <doubleClickTime>200</doubleClickTime>
  <screenEdgeWarpTime>400</screenEdgeWarpTime>
  <screenEdgeWarpMouse>false</screenEdgeWarpMouse>

  <context name="Frame">
    <mousebind button="A-Left" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
    </mousebind>
    <mousebind button="A-Left" action="Drag">
      <action name="Move"/>
    </mousebind>
    <mousebind button="A-Right" action="Drag">
      <action name="Resize"/>
    </mousebind>
  </context>

  <context name="Titlebar">
    <mousebind button="Left" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
    </mousebind>
    <mousebind button="Left" action="Drag">
      <action name="Move"/>
    </mousebind>
    <mousebind button="Left" action="DoubleClick">
      <action name="ToggleMaximizeFull"/>
    </mousebind>
    <mousebind button="Right" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
      <action name="ShowMenu"><menu>client-menu</menu></action>
    </mousebind>
  </context>

  <!-- Without a Client context openbox never grabs button presses on a
       window's body, so tapping a background window does nothing at all:
       no focus, no raise. With followMouse=no the only other ways to focus
       are the ~20px titlebar and the taskbar — a bad trade on a touchscreen
       head unit where tapping the window IS the primary gesture. Verbatim
       from stock /etc/xdg/openbox/rc.xml; the press still passes through to
       the application. -->
  <context name="Client">
    <mousebind button="Left" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
    </mousebind>
    <mousebind button="Middle" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
    </mousebind>
    <mousebind button="Right" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
    </mousebind>
  </context>

  <!-- titleLayout NLIMC renders an icon button; without this context it is
       dead. This is also the only pointer route to the per-window menu
       (Move/Resize/layer/Send to/Close). -->
  <context name="Icon">
    <mousebind button="Left" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
      <action name="ShowMenu"><menu>client-menu</menu></action>
    </mousebind>
    <mousebind button="Right" action="Press">
      <action name="Focus"/>
      <action name="Raise"/>
      <action name="ShowMenu"><menu>client-menu</menu></action>
    </mousebind>
  </context>

  <context name="Desktop">
    <mousebind button="Right" action="Press">
      <action name="ShowMenu">
        <menu>root-menu</menu>
      </action>
    </mousebind>
    <mousebind button="Middle" action="Press">
      <action name="ShowMenu">
        <menu>client-list-combined-menu</menu>
      </action>
    </mousebind>
  </context>

  <context name="Close"><mousebind button="Left" action="Click"><action name="Close"/></mousebind></context>
  <context name="Maximize"><mousebind button="Left" action="Click"><action name="ToggleMaximizeFull"/></mousebind></context>
  <context name="Iconify"><mousebind button="Left" action="Click"><action name="Iconify"/></mousebind></context>
</mouse>

<menu>
  <file>/home/archer/.config/openbox/menu.xml</file>
  <hideDelay>200</hideDelay>
  <middle>no</middle>
  <submenuShowDelay>100</submenuShowDelay>
  <showIcons>yes</showIcons>
  <manageDesktops>no</manageDesktops>
</menu>

<applications>
  <application class="Chromium*">
    <maximized>yes</maximized>
    <focus>yes</focus>
    <desktop>1</desktop>
  </application>
</applications>

</openbox_config>
RCXML

# openbox right-click root menu — element names/order checked against
# openbox's real menu.xsd. Restart/Power Off use the same PID-1 signal
# protocol archer_init.c implements (SIGUSR1 = reboot, SIGUSR2 = poweroff)
# — the same mechanism used throughout tonight's manual VM testing.
cat > "$MOUNT/home/archer/.config/openbox/menu.xml" <<'MENUXML'
<?xml version="1.0" encoding="UTF-8"?>

<openbox_menu xmlns="http://openbox.org/3.4/menu">

<menu id="root-menu" label="ARCHER">
  <item label="Dashboard">
    <action name="Execute">
      <command>/opt/archer/desktop-dashboard.sh</command>
    </action>
  </item>
  <item label="Terminal">
    <action name="Execute">
      <command>lxterminal</command>
    </action>
  </item>
  <item label="Files">
    <action name="Execute">
      <command>pcmanfm</command>
    </action>
  </item>
  <separator/>
  <item label="Reload Desktop">
    <action name="Execute">
      <command>openbox --reconfigure</command>
    </action>
  </item>
  <separator label="Power"/>
  <item label="Restart Archer OS">
    <action name="Execute">
      <command>sudo kill -USR1 1</command>
    </action>
  </item>
  <item label="Power Off">
    <action name="Execute">
      <command>sudo kill -USR2 1</command>
    </action>
  </item>
</menu>

</openbox_menu>
MENUXML

# tint2 taskbar theme — every directive cross-checked against this exact
# tint2 version's own shipped example configs
# (/etc/xdg/tint2/tint2rc and /usr/share/tint2/horizontal-dark-opaque.tint2rc
# inside the tint2 .deb), not assumed. tint2 only supports one
# task_font_color for all states (no active/urgent/iconified variants —
# confirmed absent from both real examples), so state is conveyed via
# background_id (background/border color per numbered background block)
# instead. Colors match archer_dashboard.html's own :root CSS variables.
mkdir -p "$MOUNT/home/archer/.config/tint2"
cat > "$MOUNT/home/archer/.config/tint2/tint2rc" <<'TINT2RC'
#---- Generated for Archer OS -----
#-------------------------------------
# Background 1: panel body
rounded = 0
border_width = 0
border_sides = TBLR
background_color = #050508 100
border_color = #16162a 100
background_color_hover = #050508 100
border_color_hover = #16162a 100
background_color_pressed = #050508 100
border_color_pressed = #16162a 100

# Background 2: default task, iconified task
rounded = 4
border_width = 1
border_sides = TBLR
background_color = #0a0a12 100
border_color = #1e1e35 100
background_color_hover = #0d0d16 100
border_color_hover = #009ab5 100
background_color_pressed = #0d0d16 100
border_color_pressed = #00e5ff 100

# Background 3: active task
rounded = 4
border_width = 1
border_sides = TBLR
background_color = #0d0d16 100
border_color = #00e5ff 100
background_color_hover = #0d0d16 100
border_color_hover = #00e5ff 100
background_color_pressed = #0d0d16 100
border_color_pressed = #00e5ff 100

# Background 4: urgent task
rounded = 4
border_width = 1
border_sides = TBLR
background_color = #1a0000 100
border_color = #ff3333 100
background_color_hover = #1a0000 100
border_color_hover = #ff3333 100
background_color_pressed = #1a0000 100
border_color_pressed = #ff3333 100

#-------------------------------------
# Panel
panel_items = LTSC
panel_size = 100% 34
panel_margin = 0 0
panel_padding = 6 0 6
panel_background_id = 1
panel_dock = 0
panel_position = bottom center horizontal
panel_layer = top
panel_monitor = all
autohide = 0
strut_policy = follow_size
disable_transparency = 0
mouse_effects = 1
font_shadow = 0
mouse_hover_icon_asb = 100 0 10
mouse_pressed_icon_asb = 100 0 0

#-------------------------------------
# Taskbar
taskbar_mode = single_desktop
taskbar_hide_if_empty = 0
taskbar_padding = 4 2 4
taskbar_background_id = 0
taskbar_active_background_id = 0
taskbar_name = 0
taskbar_distribute_size = 0
taskbar_sort_order = none
task_align = left

#-------------------------------------
# Task
task_text = 1
task_icon = 1
task_centered = 1
urgent_nb_of_blink = 8
task_maximum_size = 200 32
task_padding = 8 2 4
task_font = Share Tech Mono 9
task_tooltip = 1
task_thumbnail = 0
task_font_color = #dde4e8 100
task_background_id = 2
task_active_background_id = 3
task_urgent_background_id = 4
task_iconified_background_id = 2
mouse_left = toggle_iconify
mouse_middle = none
mouse_right = close
mouse_scroll_up = prev_task
mouse_scroll_down = next_task

#-------------------------------------
# Launcher — Dashboard / Terminal / Files, always visible on the panel.
# Directive names taken from this tint2 version's own shipped example
# configs, same as the rest of this file.
launcher_padding = 6 6 6
launcher_background_id = 0
launcher_icon_background_id = 0
launcher_icon_size = 22
launcher_icon_asb = 100 0 0
launcher_icon_theme_override = 0
startup_notifications = 1
launcher_tooltip = 1
launcher_item_app = /usr/share/applications/archer-dashboard.desktop
launcher_item_app = /usr/share/applications/archer-terminal.desktop
launcher_item_app = /usr/share/applications/archer-files.desktop

#-------------------------------------
# System tray (notification area)
systray_padding = 4 4 4
systray_background_id = 0
systray_sort = ascending
systray_icon_size = 20
systray_icon_asb = 100 0 0
systray_monitor = 1
systray_name_filter =

#-------------------------------------
# Clock
time1_format = %H:%M
time1_font = Orbitron bold 10
time2_format = %a %b %d
time2_font = Share Tech Mono 8
time1_timezone =
time2_timezone =
clock_font_color = #00e5ff 100
clock_padding = 8 0
clock_background_id = 0
clock_tooltip =
clock_tooltip_timezone =
clock_lclick_command =
clock_rclick_command =
clock_mclick_command =
clock_uwheel_command =
clock_dwheel_command =

#-------------------------------------
# Battery — no battery on a truck head unit
battery_tooltip = 0
battery_low_status = 0
TINT2RC

# ── Archer openbox theme ──────────────────────────────────────────────
# openbox themes are pure text (the stock Onyx theme ships exactly one
# file, themerc, and no images — checked inside the real .deb), so a
# fully branded window-decoration theme costs nothing and needs no
# artwork. Colors are the dashboard's own :root CSS variables. Lives in
# /usr/share/themes rather than the archer user's home so it resolves
# no matter which uid ends up running the session.
mkdir -p "$MOUNT/usr/share/themes/Archer/openbox-3"
cat > "$MOUNT/usr/share/themes/Archer/openbox-3/themerc" <<'THEMERC'
!! Archer OS — matches archer_dashboard.html's palette
!! bg #050508 / panel #0a0a12 / border #16162a / accent cyan #00e5ff

border.width: 1
padding.width: 6
padding.height: 3
window.handle.width: 0
menu.overlap: 0

!! ── Window borders — focused window gets the cyan accent ──
window.active.border.color: #00e5ff
window.inactive.border.color: #16162a
window.active.client.color: #0a0a12
window.inactive.client.color: #050508

!! ── Titlebar ──
window.active.title.bg: flat solid
window.active.title.bg.color: #0d0d16
window.inactive.title.bg: flat solid
window.inactive.title.bg.color: #050508
window.inactive.title.separator.color: #16162a

!! ── Titlebar text ──
window.label.text.justify: center
window.active.label.bg: parentrelative
window.active.label.text.color: #00e5ff
window.inactive.label.bg: parentrelative
window.inactive.label.text.color: #6a7a88

!! ── Window buttons ──
window.*.button.*.bg: parentrelative
window.active.button.*.image.color: #6a7a88
window.inactive.button.*.image.color: #30394a
window.active.button.*.hover.bg: flat solid
window.active.button.*.hover.bg.color: #0a0a12
window.active.button.*.hover.image.color: #00e5ff
window.inactive.button.*.hover.bg: parentrelative
window.inactive.button.*.hover.image.color: #6a7a88
window.*.button.*.pressed.bg: flat solid
window.active.button.*.pressed.bg.color: #00394a
window.inactive.button.*.pressed.bg.color: #0a0a12
window.active.button.*.pressed.image.color: #00e5ff
window.inactive.button.*.pressed.image.color: #6a7a88
window.active.button.disabled.image.color: #1e1e35
window.inactive.button.disabled.image.color: #1e1e35
!! No per-button styling here on purpose: obrender/theme.c reads button
!! appearance through a single global key (window.active.button.hover.bg
!! and .image.color), so a per-button override like
!! window.active.button.close.hover.bg is never queried and is silently
!! dead. themerc is parsed as an XrmDatabase, which is why the '*' forms
!! above work — they are loose bindings that match openbox's own lookups.

!! ── Menu ──
menu.border.color: #1e1e35
menu.title.bg: flat solid
menu.title.bg.color: #050508
menu.title.text.color: #00e5ff
menu.title.text.justify: center
menu.items.bg: flat solid
menu.items.bg.color: #0a0a12
menu.items.text.color: #dde4e8
menu.items.justify: left
menu.items.disabled.text.color: #30394a
menu.items.active.bg: flat solid
menu.items.active.bg.color: #00394a
menu.items.active.text.color: #00e5ff
menu.separator.color: #1e1e35

!! ── On-screen display (resize/move popups, alt-tab) ──
osd.bg: flat solid
osd.bg.color: #0a0a12
osd.border.color: #00e5ff
osd.label.bg: parentrelative
osd.label.text.color: #00e5ff
osd.hilight.bg: flat solid
osd.hilight.bg.color: #00e5ff
osd.unhilight.bg: flat solid
osd.unhilight.bg.color: #1e1e35
THEMERC

# ── Launcher entries for the taskbar ──────────────────────────────────
# tint2's launcher takes .desktop files. Writing our own three rather
# than pointing at the packages' (whose filenames/paths vary by distro
# and version) keeps this from silently losing an icon on a rebuild.
# Icon names are freedesktop standard ones that adwaita-icon-theme (a
# hard dependency of GTK, so always present) ships as raster PNGs.
mkdir -p "$MOUNT/usr/share/applications"
cat > "$MOUNT/usr/share/applications/archer-dashboard.desktop" <<'DESKTOP1'
[Desktop Entry]
Type=Application
Name=Dashboard
Comment=Archer truck dashboard
Exec=/opt/archer/desktop-dashboard.sh
Icon=utilities-system-monitor
Terminal=false
Categories=System;
DESKTOP1
cat > "$MOUNT/usr/share/applications/archer-terminal.desktop" <<'DESKTOP2'
[Desktop Entry]
Type=Application
Name=Terminal
Comment=Command line
Exec=lxterminal
Icon=utilities-terminal
Terminal=false
Categories=System;
DESKTOP2
cat > "$MOUNT/usr/share/applications/archer-files.desktop" <<'DESKTOP3'
[Desktop Entry]
Type=Application
Name=Files
Comment=Browse files
Exec=pcmanfm
Icon=system-file-manager
Terminal=false
Categories=System;
DESKTOP3

chroot "$MOUNT" chown -R archer:archer /home/archer/.config

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
    # Run the session UNPRIVILEGED. xserver-xorg-legacy (installed above)
    # ships the setuid-root /usr/lib/xorg/Xorg.wrap, and Xwrapper.config
    # grants allowed_users=anybody + needs_root_rights=yes — that is
    # exactly the supported way to let a normal user start X while only
    # the X server itself gets root. So openbox, tint2, Chromium, pcmanfm
    # and lxterminal all run as archer rather than as uid 0.
    #
    # This is evidence-backed rather than hopeful: on this very image,
    # after xserver-xorg-legacy was installed, a plain unprivileged
    # `startx` was run by hand and Xorg came up as uid=0 via the wrapper
    # (visible in the dbus log line naming /usr/lib/xorg/Xorg with uid=0).
    #
    # The fallback below is the safety net: if the unprivileged path dies
    # almost immediately, that means the wrapper did not take, so retry
    # the old root path rather than leaving a black screen. A real
    # session always lasts far longer than this threshold, so a normal
    # logout/exit will not trigger the retry.
    _archer_t0=$(date +%s)
    startx /opt/archer/kiosk.sh -- :0 vt1 >/tmp/archer-x.log 2>&1
    _archer_elapsed=$(( $(date +%s) - _archer_t0 ))
    if [ "$_archer_elapsed" -lt 10 ]; then
        echo "[archer] unprivileged startx exited after ${_archer_elapsed}s — falling back to sudo" >> /tmp/archer-x.log
        sudo startx /opt/archer/kiosk.sh -- :0 vt1 >>/tmp/archer-x.log 2>&1
    fi
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
# Single source of truth for the kernel command line. It feeds BOTH the
# hand-written "Archer OS" menuentry (config/grub.cfg, substituted just
# below) and the auto-generated 10_linux entries (/etc/default/grub).
# Keeping one variable is the point: a hand-written menuentry supplies its
# own `linux` line and does NOT inherit GRUB_CMDLINE_LINUX_DEFAULT, while
# GRUB_DEFAULT=archer-os makes that hand-written entry the one that boots.
# They had already drifted, and it cost a real hardware boot.
#
# Verbose rather than "quiet loglevel=0" deliberately: build.sh has not yet
# been confirmed booting end to end on hardware, and loglevel=0 hides
# panics and hangs — the exact trap already hit once on the VM variant.
# Switch to quiet once a USB boot is confirmed reaching the desktop.
ARCHER_CMDLINE="loglevel=7 ignore_loglevel vga=791 video=efifb:off video=vesafb:off init=/sbin/archer_init"

sed -e "s/__ARCHER_KERNEL_VERSION__/${ARCHER_KERNEL_VER}/g" \
    -e "s|__ARCHER_CMDLINE__|${ARCHER_CMDLINE}|g" \
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
#
# video=efifb:off video=vesafb:off — found next, still on the VM variant:
# even with a genuinely valid framebuffer active (confirmed via dmesg —
# real address, correct size, correct mode), Xorg's old xf86-video-fbdev
# driver still failed with "no screens found" against it. Ruled out
# permissions, PCI/GPU auto-bind matching, and the config file itself
# before concluding it's a real fbdev-driver/efifb compatibility gap, not
# something fixable from the X config side. These stop efifb/vesafb from
# claiming the boot framebuffer at all, so CONFIG_DRM_SIMPLEDRM=y (see
# kernel/archer.config — built in, not a module, specifically because
# there's no udev here to modprobe anything and it needs to already be
# active at the same early boot stage efifb/vesafb would have claimed,
# with no gap where nothing's bound) gets it instead, and Xorg's modern
# "modesetting" driver uses that DRM device rather than fbdev.
cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=archer-os
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="${ARCHER_CMDLINE}"
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
