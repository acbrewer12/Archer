#!/bin/bash
# One-time OBDLink MX+ Bluetooth pairing helper for archer-os.
#
# Run this ONCE, manually, via the tty2 maintenance shell (Ctrl+Alt+F2):
#   sudo bash /opt/archer/archer-os/obd-auth/obd_bt_pair.sh
#
# It scans for nearby Bluetooth devices, lets you pick the OBDLink MX+,
# pairs and trusts it, then writes its MAC address to
# /etc/archer/obd_bt_mac so obd_bt_bind.sh can re-bind /dev/rfcomm0 to it
# on every subsequent boot without re-pairing.
#
# NOT verified against a real OBDLink MX+ or a real bluetoothd — no
# Bluetooth hardware or booted VM was available while this was written.
# Written against BlueZ's documented bluetoothctl behavior; confirm on
# real hardware before trusting it unattended. Make sure the OBDLink MX+
# is powered and in pairing mode per its own manual before running this.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

echo "Powering on the Bluetooth adapter..."
# Same risk as `devices` below, applied proactively rather than waiting to
# confirm each one hangs individually: these are one-shot adapter-state
# commands with no legitimate reason to ever take more than a moment, so
# there's nothing to lose by guarding them the same way.
#
# Confirmed live: the timeouts themselves work (exit 124 when bluetoothd
# isn't reachable), but under set -e that killed the script silently —
# this is a manual, tty2-shell tool, so a person needs to see WHY it
# stopped, not just land back at a bare prompt. archer-os has no systemd
# (archer_init.c is PID 1 — see its header), so there's no `systemctl
# status bluetooth` to point at; archer_init's own bluetoothd start/exit
# log lines go to /run/archer_init.log and dmesg instead (see ilog() in
# archer_init.c) — that's what these messages point to.
timeout 5 bluetoothctl power on || {
    echo "ERROR: bluetoothctl power on timed out — is bluetoothd running at all?" >&2
    echo "Check: ps aux | grep bluetoothd   or   cat /run/archer_init.log" >&2
    exit 1
}
timeout 5 bluetoothctl agent on || {
    echo "ERROR: bluetoothctl agent on timed out — bluetoothd isn't responding." >&2
    echo "Check: ps aux | grep bluetoothd   or   cat /run/archer_init.log" >&2
    exit 1
}
timeout 5 bluetoothctl default-agent || {
    echo "ERROR: bluetoothctl default-agent timed out — bluetoothd isn't responding." >&2
    echo "Check: ps aux | grep bluetoothd   or   cat /run/archer_init.log" >&2
    exit 1
}

echo "Scanning for 15 seconds — make sure the OBDLink MX+ is powered and discoverable..."
timeout 15 bluetoothctl scan on || true

echo
echo "Discovered devices:"
# Confirmed hanging indefinitely on a VM with no working bluetoothd (found
# via `ps aux` — the process just sat there, unlike `scan on` above, which
# has its own timeout and completes fine even when nothing is discovered).
# Unlike that one, a timeout here is a real failure, not a normal way for
# the command to end — no `|| true`, let set -e stop the script rather
# than falling through to a MAC prompt with nothing to actually pick from.
timeout 5 bluetoothctl devices || {
    echo "ERROR: bluetoothctl devices timed out — is bluetoothd actually running?" >&2
    echo "Check: ps aux | grep bluetoothd   or   cat /run/archer_init.log" >&2
    exit 1
}
echo

read -rp "Enter the OBDLink MX+'s MAC address from the list above: " MAC

if ! [[ "$MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    echo "That doesn't look like a MAC address (expected XX:XX:XX:XX:XX:XX)." >&2
    exit 1
fi

echo "Pairing with $MAC..."
bluetoothctl pair "$MAC"
bluetoothctl trust "$MAC"

mkdir -p /etc/archer
echo "$MAC" > /etc/archer/obd_bt_mac
chown root:archer /etc/archer/obd_bt_mac
chmod 640 /etc/archer/obd_bt_mac

echo "Paired and saved. /etc/archer/obd_bt_mac now holds $MAC."
echo "Reboot (or run obd_bt_bind.sh directly) to bind /dev/rfcomm0."
