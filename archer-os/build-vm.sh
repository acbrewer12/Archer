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
# xserver-xorg-legacy is required, not optional — found by actually booting
# this image: without it, /usr/lib/xorg/Xorg.wrap doesn't exist, so
# /usr/bin/Xorg's own dispatcher script falls through to exec'ing the real
# Xorg binary directly with none of Xwrapper.config's settings (below)
# applied. Plain `startx` then runs Xorg as whatever user invoked it,
# failing immediately with "_XSERVTransmkdir: ERROR: euid != 0" — sudo
# startx (see .bash_profile below) covers this too, but installing the
# actual package this config already assumes exists is the real fix.
DEBIAN_FRONTEND=noninteractive chroot "$MOUNT" apt-get install -y -qq \
    --no-install-recommends \
    xorg xinit xserver-xorg-legacy chromium x11-xserver-utils fontconfig \
    openbox tint2 pcmanfm lxterminal 2>&1 || \
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

# modesetting driver, not fbdev — found by actually booting this image:
# even with a genuinely valid kernel framebuffer active (confirmed via
# dmesg), Xorg's old xf86-video-fbdev driver still failed with "no screens
# found" against it — a real fbdev-driver/efifb compatibility gap, not
# something fixable from this config file (permissions, PCI/GPU auto-bind
# matching, and the fbdev device path were all ruled out first). modesetting
# is Xorg's modern, generally more robust driver, and works against the
# CONFIG_DRM_SIMPLEDRM=y device (see kernel/archer.config and the
# video=efifb:off/video=vesafb:off GRUB parameters below) instead of the
# kernel's raw /dev/fb0.
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

# Desktop session — real openbox window manager + tint2 taskbar +
# pcmanfm/lxterminal, replacing the old single-app --kiosk Chromium
# lock-in. kiosk.sh keeps its name (referenced by .bash_profile/.xinitrc
# below) but is now the desktop session launcher, not the app launcher —
# it waits for Flask, then hands off to openbox; openbox itself sources
# ~/.config/openbox/autostart, which launches tint2 and the dashboard.
mkdir -p "$MOUNT/opt/archer"
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

# Wait up to 45s for Flask to be ready. Pure-bash TCP probe — curl is not
# guaranteed to be present this early (and isn't worth the dependency here).
for i in $(seq 1 45); do
    { exec 3<>/dev/tcp/127.0.0.1/5000; } 2>/dev/null && { exec 3<&- 3>&-; break; }
    sleep 1
done
# Disable screensaver / power management
xset s off -dpms 2>/dev/null || true

# .bash_profile launches this via `sudo startx`, so this script and every
# process it spawns runs as ROOT with HOME=/root — verified empirically
# with a real NOPASSWD-sudo test user, not assumed (sudo's env_reset
# resets HOME to the target user's home). openbox and tint2 both discover
# their config via $HOME/.config, so without this they would read
# /root/.config/... , find nothing, and silently fall back to stock
# defaults: no taskbar, no dashboard, no theme, generic Debian root menu.
# Point HOME at the archer config tree the build actually wrote.
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
# Chromium reuses its existing window automatically on repeat launches
# against the same --user-data-dir, so calling this again from the menu
# to "get back to the dashboard" is safe, not a duplicate-window bug.
cat > "$MOUNT/opt/archer/desktop-dashboard.sh" <<'DASHSCRIPT'
#!/bin/bash
mkdir -p /home/archer/.config/archer-chrome 2>/dev/null || true

URL="http://127.0.0.1:5000/dashboard"
LOG=/tmp/archer-chromium.log
: > "$LOG"

CHROME_FLAGS=(
    --app="$URL"
    --start-maximized
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
    # simpledrm (see kernel/archer.config) provides mode-setting only, not
    # real GPU/DRI acceleration — letting Chromium try GPU compositing
    # crashes its GPU process and leaves a blank black window. Force
    # software rendering/compositing instead.
    --disable-gpu
    --disable-gpu-compositing
    --use-gl=swiftshader
)
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
  <name>Onyx</name>
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
panel_items = TSC
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

chroot "$MOUNT" chown -R archer:archer /home/archer/.config
chmod +x "$MOUNT/opt/archer/kiosk.sh"

# .bash_profile — on tty1 (physical display), start X kiosk automatically
cat > "$MOUNT/home/archer/.bash_profile" <<'BASHPROFILE'
# tty1 = kiosk display (dashboard). tty2 = maintenance shell (Ctrl+Alt+F2).
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$DISPLAY" ]; then
    # Don't exec — keep bash alive so if X exits we drop to a shell instead
    # of dying and triggering an infinite getty restart loop.
    #
    # sudo is required here, not optional — found by actually booting this
    # image: Xorg's setuid bit is deliberately stripped above (0755 instead
    # of 4755, see the dpkg-statoverride comment) specifically so it doesn't
    # rely on setuid working, with the archer user's NOPASSWD sudo meant to
    # take its place — but this line called `startx` plain, so Xorg ran as
    # an ordinary unprivileged user and failed immediately with
    # "_XSERVTransmkdir: ERROR: euid != 0" (confirmed via /tmp/archer-x.log
    # on a real boot), cascading into a generic "no screens found" that had
    # nothing to do with the framebuffer/vga= fix above.
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
    # /opt/archer already has kiosk.sh in it (written earlier, above) —
    # `git clone` refuses to target a non-empty directory, so clone into a
    # scratch dir and merge its contents in instead of cloning in place.
    rm -rf "$MOUNT/opt/archer.clone"
    git clone \
        --branch "$ARCHER_BRANCH" --depth 1 \
        "$ARCHER_REPO" "$MOUNT/opt/archer.clone"
    cp -a "$MOUNT/opt/archer.clone/." "$MOUNT/opt/archer/"
    rm -rf "$MOUNT/opt/archer.clone"
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

# archer_init.c runs the OBD2 auth handshake as root and refuses to exec
# anything that isn't root-owned/non-writable first (fail-closed check added
# after a prior security fix). The chown -R above just re-owned this script
# to 'archer' along with the rest of /opt/archer, which would make that
# check always fail and silently disable OBD2 auth on every boot. Re-root
# it specifically.
#
# NOTE: /opt/archer/.venv/bin/python3 is NOT re-chowned here on purpose —
# `python3 -m venv` creates it as a symlink to the system python3 outside
# /opt/archer, and chown -R re-owns the symlink itself but not its target
# (verified empirically), while archer_init.c's check uses stat(), which
# follows the symlink and already sees the untouched, root-owned system
# interpreter. If venv creation ever changes to --copies, this needs the
# same treatment as the line below.
chroot "$MOUNT" chown root:root /opt/archer/archer-os/obd-auth/obd_auth_client.py
chroot "$MOUNT" chmod 644 /opt/archer/archer-os/obd-auth/obd_auth_client.py

# /etc/archer holds both root-only secrets (obd_auth.key, used by the root-run
# boot-time OBD auth handshake) and files archer.py (running as the unprivileged
# 'archer' user) must read/write at runtime (archer.env, and hsm.py's master.key
# created on first run). Group-own it with the sticky bit set (like /tmp) so the
# archer group can create/read its own files without being able to delete or
# overwrite root-owned ones such as obd_auth.key.
mkdir -p "$MOUNT/etc/archer"
chroot "$MOUNT" chown root:archer /etc/archer
chroot "$MOUNT" chmod 1770 /etc/archer

# Embed OBD2 auth key if one has been generated (see archer-os/obd-auth/keygen.sh).
# The key is in .gitignore and must be generated separately and kept secret.
# Root-owned, no group access — archer.py must never be able to read this;
# only the root-run boot-time OBD auth handshake needs it.
KEY_SRC="$(dirname "$0")/obd-auth/obd_auth.key"
if [ -f "$KEY_SRC" ]; then
    cp "$KEY_SRC" "$MOUNT/etc/archer/obd_auth.key"
    chmod 600 "$MOUNT/etc/archer/obd_auth.key"
    log "OBD2 auth key installed"
else
    log "No OBD2 auth key found — run archer-os/obd-auth/keygen.sh to generate one"
fi

# Embed API key config if it exists — contains GEMINI_API_KEY etc.
# Format: KEY=value, one per line. Never committed to git (.gitignore protected).
# Create: archer-os/archer.env  with  GEMINI_API_KEY=your_key_here
# Group-readable by archer (archer.py reads this file directly at startup —
# see archer.py's own env file loader) but not group-writable, so the sticky
# bit on /etc/archer still protects it from being overwritten by that same
# unprivileged process.
ENV_SRC="$(dirname "$0")/archer.env"
if [ -f "$ENV_SRC" ]; then
    cp "$ENV_SRC" "$MOUNT/etc/archer/archer.env"
    chroot "$MOUNT" chown root:archer /etc/archer/archer.env
    chmod 640 "$MOUNT/etc/archer/archer.env"
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
#
# vga=791 (1024x768, 16-bit) — found missing by actually booting this image
# in QEMU: CONFIG_FB_VESA=y being compiled into the kernel only means the
# driver exists, not that it activates. Under legacy BIOS boot (what QEMU's
# default SeaBIOS does without -bios ovmf), vesafb needs an explicit vga=
# mode number or it never creates /dev/fb0 at all, which made Xorg's fbdev
# driver fail with "no screens found" — confirmed via /proc/cmdline and
# dmesg on a real boot, not assumed. Also applied to build.sh, since it has
# the identical gap for the same reason on real hardware booting legacy BIOS.
#
# video=efifb:off video=vesafb:off — this VM actually boots UEFI (confirmed
# via /proc/fb reporting "EFI VGA" and /sys/firmware/efi existing, despite
# no -bios ovmf being passed to QEMU explicitly), so efifb — not vesafb —
# was the one holding the framebuffer. Its data was completely valid
# (confirmed via dmesg: real address, 1536k, correct 1024x768x16 mode) but
# Xorg's old xf86-video-fbdev driver still failed with "no screens found"
# against it — ruled out permissions (Xorg runs as root, see the sudo
# startx comment below), PCI/GPU auto-bind matching (AutoAddGPU/
# AutoEnableDevices "false" made no difference), and the config file
# (explicit Option "fbdev" "/dev/fb0" made no difference either) before
# concluding this is a real fbdev-driver/efifb compatibility gap, not
# something fixable from the config side. These two parameters stop
# efifb/vesafb from claiming the boot framebuffer at all, so the
# CONFIG_DRM_SIMPLEDRM=y driver (see kernel/archer.config) gets it instead
# — built in rather than a module specifically because there's no udev
# here to modprobe anything, so it needs to already be active at the same
# early boot stage efifb/vesafb would have claimed it, with no gap where
# nothing's bound (a module-loaded-after-boot simpledrm was tried live and
# left the console completely blind, both ttys, since nothing else took
# over rendering it — don't repeat that by making these two changes
# independently of the kernel config change above).
cat > "$MOUNT/etc/default/grub" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="Archer OS"
GRUB_CMDLINE_LINUX_DEFAULT="loglevel=7 ignore_loglevel vga=791 video=efifb:off video=vesafb:off init=/sbin/archer_init"
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
  Logs:     cat /run/archer_init.log
  Boot log: cat /var/log/archer_boot_dmesg.log
  Update:   sudo /opt/archer/update.sh
  Status:   cat /run/archer_status

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
