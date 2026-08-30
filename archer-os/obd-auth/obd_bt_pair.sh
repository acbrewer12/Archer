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
bluetoothctl power on
bluetoothctl agent on
bluetoothctl default-agent

echo "Scanning for 15 seconds — make sure the OBDLink MX+ is powered and discoverable..."
timeout 15 bluetoothctl scan on || true

echo
echo "Discovered devices:"
bluetoothctl devices
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
