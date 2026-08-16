#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  ARCHER OS — console login (runs on tty1, before X starts)
#
#  WHY THIS EXISTS, and why the login is not just a web page:
#
#  Chromium on this hardware has no GPU acceleration — simpledrm is
#  mode-setting only, so everything renders through SwiftShader in
#  software. Its first paint is many seconds away. A login screen that
#  waits for X + openbox + Chromium + Flask therefore cannot be fast, no
#  matter how much of the boot is trimmed behind it.
#
#  So the PIN is asked for here instead: this runs the moment agetty
#  hands tty1 to a shell, roughly a second into userspace, and the whole
#  graphical stack starts only after it is satisfied. That is what makes
#  a ~3s time-to-login possible.
#
#  FIRST BOOT IS DIFFERENT ON PURPOSE. With no PIN configured this exits
#  immediately and lets the graphical first-run wizard at /setup handle
#  it, where a real keyboard-friendly UI is worth the wait. Setup happens
#  once; logging in happens every single start.
#
#  Verification is delegated to /opt/archer/pin.py — the same module and
#  the same credential file the web login uses, so there is exactly one
#  implementation of the hashing.
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

PIN_TOOL=/opt/archer/pin.py
PY="$(command -v python3 || echo /usr/bin/python3)"

# Written on success so the dashboard knows this boot is already unlocked and
# does not ask for the same PIN a second time. /run is tmpfs: it evaporates at
# power-off, so every cold boot starts locked again. That per-boot lifetime is
# the entire reason the marker lives here and not anywhere persistent.
UNLOCK_MARKER=/run/archer-console-unlock

# No credential yet -> first boot. Say nothing, get out of the way, let the
# graphical setup wizard run.
"$PY" "$PIN_TOOL" is-configured 2>/dev/null || exit 0

C='\033[38;5;51m'    # cyan, matching the dashboard accent
D='\033[38;5;242m'   # dim
R='\033[38;5;203m'   # red
N='\033[0m'

MAX_TRIES=5
LOCKOUT=30

while true; do
    tries=0
    while [ "$tries" -lt "$MAX_TRIES" ]; do
        clear 2>/dev/null || true
        echo ""
        echo -e "      ${C}█▀█ █▀█ █▀▀ █ █ █▀▀ █▀█${N}"
        echo -e "      ${C}█▀█ █▀▄ █   █▀█ █▀▀ █▀▄${N}"
        echo ""
        if [ "$tries" -gt 0 ]; then
            echo -e "      ${R}Incorrect PIN${N}  ${D}($((MAX_TRIES - tries)) left)${N}"
        else
            echo -e "      ${D}enter PIN to unlock${N}"
        fi
        echo ""
        printf "      PIN: "

        # -s so the PIN is not echoed to a screen mounted in a windshield.
        read -r -s pin
        echo ""

        if printf '%s\n' "$pin" | "$PY" "$PIN_TOOL" verify 2>/dev/null; then
            unset pin
            # Root-owned (archer has NOPASSWD sudo — see build.sh) so the
            # marker cannot be dropped by anything that did not clear this
            # prompt. If sudo somehow fails, the driver simply meets the web
            # login as well: mildly annoying, never locked out.
            sudo touch "$UNLOCK_MARKER" 2>/dev/null || true
            echo -e "      ${C}unlocked${N}"
            exit 0
        fi
        unset pin
        tries=$((tries + 1))
        sleep 1
    done

    # Throttle rather than lock out permanently: this is a vehicle, and a
    # driver who cannot get into their own truck is a worse outcome than a
    # slow brute force. Physical access is root on this system anyway (see
    # the maintenance shell on tty2), so this is a speed bump by design, not
    # a security boundary.
    clear 2>/dev/null || true
    echo ""
    echo -e "      ${R}Too many attempts${N}"
    for s in $(seq "$LOCKOUT" -1 1); do
        printf "\r      ${D}locked — %2ds${N}  " "$s"
        sleep 1
    done
    echo ""
done
