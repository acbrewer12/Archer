#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  ARCHER OS — system settings
#
#  The dashboard has its own in-app settings tab for anything the web app
#  owns (vehicle, display, AI keys). This covers the system-level things
#  it structurally cannot: network, disk install, the login PIN, logs,
#  and power. Deliberately a plain text menu — no dialog/whiptail
#  dependency to add to an image where every package is hand-picked.
#
#  Launched from the desktop menu / taskbar (Settings), or:
#      /opt/archer/settings.sh
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

CYN='\033[0;36m'; GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[0;90m'; BLD='\033[1m'; NC='\033[0m'
OWNER_CRED=/etc/archer/owner.json

pause() { echo ""; read -r -p "  Press Enter to return to the menu..." _; }

header() {
    clear 2>/dev/null || true
    echo ""
    echo -e "  ${CYN}${BLD}ARCHER${NC}  ${DIM}system settings${NC}"
    echo -e "  ${DIM}────────────────────────────────────────────${NC}"
}

status_line() {
    local ip wifi pin disk
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    wifi="$(nmcli -t -f NAME connection show --active 2>/dev/null | head -1)"
    [ -f "$OWNER_CRED" ] && pin="set" || pin="NOT SET"
    disk="$(findmnt -no SOURCE / 2>/dev/null)"
    echo -e "  network : ${ip:-not connected}${wifi:+  (${wifi})}"
    echo -e "  login   : PIN ${pin}"
    echo -e "  booted  : ${disk:-unknown}"
    echo -e "  ${DIM}────────────────────────────────────────────${NC}"
}

wifi_setup() {
    if ! command -v nmtui >/dev/null 2>&1; then
        echo -e "  ${RED}nmtui not installed${NC}"; pause; return
    fi
    # NetworkManager authorises non-root requests through polkit, which this
    # image does not ship — so nmtui must run as root or every change is
    # silently denied.
    sudo nmtui
}

install_to_disk() {
    header
    echo -e "  ${BLD}Install Archer OS to an internal disk${NC}"
    echo ""
    echo -e "  ${DIM}Optional. The USB stick works fine on its own; installing to an${NC}"
    echo -e "  ${DIM}internal NVMe/SSD boots faster and frees the USB port.${NC}"
    echo ""
    sudo /opt/archer/install-to-disk.sh --list
    echo -e "  ${YLW}This erases the disk you choose.${NC}"
    read -r -p "  Device to install to (blank to cancel): " dev
    [ -n "$dev" ] || { echo "  Cancelled."; pause; return; }
    sudo /opt/archer/install-to-disk.sh "$dev"
    pause
}

reset_pin() {
    header
    echo -e "  ${BLD}Reset the login PIN${NC}"
    echo ""
    if [ ! -f "$OWNER_CRED" ]; then
        echo "  No PIN is set — the next boot will run first-time setup already."
        pause; return
    fi
    echo -e "  ${DIM}This clears the stored PIN. The next time the dashboard opens it${NC}"
    echo -e "  ${DIM}runs first-time setup so you can choose a new one.${NC}"
    echo ""
    echo -e "  ${DIM}Note: anyone at this menu already has physical access to the truck,${NC}"
    echo -e "  ${DIM}and physical access is root on this system by design — so this is${NC}"
    echo -e "  ${DIM}not a new way in, just a convenient one.${NC}"
    echo ""
    read -r -p "  Type RESET to confirm: " c
    if [ "$c" = "RESET" ]; then
        sudo rm -f "$OWNER_CRED" && echo -e "  ${GRN}PIN cleared.${NC} Setup runs on the next dashboard load."
    else
        echo "  Cancelled."
    fi
    pause
}

view_logs() {
    header
    echo -e "  ${BLD}Logs${NC}   ${DIM}(q to quit the viewer)${NC}"
    echo ""
    echo "    1) Backend  — why archer.py did or didn't start   (/run/archer.log)"
    echo "    2) Boot     — what init did                       (/run/archer_init.log)"
    echo "    3) Kernel   — full boot messages                  (dmesg)"
    echo ""
    read -r -p "  Choose (blank to cancel): " l
    case "$l" in
        1) sudo less +G /run/archer.log 2>/dev/null || echo "  (no backend log yet)" ;;
        2) sudo less +G /run/archer_init.log 2>/dev/null || echo "  (no init log yet)" ;;
        3) sudo dmesg | less +G ;;
        *) return ;;
    esac
}

power_menu() {
    header
    echo -e "  ${BLD}Power${NC}"
    echo ""
    echo "    1) Restart"
    echo "    2) Shut down"
    echo ""
    read -r -p "  Choose (blank to cancel): " p
    # archer_init is PID 1 and implements its own signal protocol:
    # SIGUSR1 = reboot, SIGUSR2 = power off. There is no systemctl here.
    case "$p" in
        1) echo "  Restarting..."; sudo kill -USR1 1 ;;
        2) echo "  Shutting down..."; sudo kill -USR2 1 ;;
        *) return ;;
    esac
}

while true; do
    header
    status_line
    echo ""
    echo "    1) WiFi / network"
    echo "    2) Install to internal disk (NVMe / SSD)"
    echo "    3) Reset login PIN"
    echo "    4) View logs"
    echo "    5) Power"
    echo ""
    echo "    q) Close"
    echo ""
    read -r -p "  Choose: " choice
    case "$choice" in
        1) wifi_setup ;;
        2) install_to_disk ;;
        3) reset_pin ;;
        4) view_logs ;;
        5) power_menu ;;
        q|Q|"") exit 0 ;;
    esac
done
