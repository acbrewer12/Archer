#!/bin/bash
# Boot-time rfcomm bind for a previously-paired OBDLink MX+.
#
# Run automatically by archer_init.c, as root, right after bluetoothd
# starts. Requires obd_bt_pair.sh to have been run once already (it writes
# the paired MAC to /etc/archer/obd_bt_mac) — if that file doesn't exist,
# this exits quietly: no pairing has happened yet, nothing to bind, not an
# error. Once bound, archer.py picks the adapter up via OBD_PORT=/dev/rfcomm0
# in archer.env (see obd_autodetect() in archer.py), not autodetection —
# a Bluetooth RFCOMM device doesn't expose the description/manufacturer
# strings autodetection scans for.
#
# NOT verified against real hardware — see obd_bt_pair.sh's header for why.

MAC_FILE="/etc/archer/obd_bt_mac"

if [ ! -f "$MAC_FILE" ]; then
    echo "obd_bt_bind: $MAC_FILE not found — no OBDLink paired yet, nothing to bind"
    exit 0
fi

MAC="$(cat "$MAC_FILE")"

if ! [[ "$MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    echo "obd_bt_bind: $MAC_FILE does not contain a valid MAC address, skipping" >&2
    exit 0
fi

# Release any stale binding left over from an unclean shutdown before
# re-binding — a leftover bind makes the next `rfcomm bind` fail with
# "Device or resource busy".
rfcomm release rfcomm0 >/dev/null 2>&1 || true

if rfcomm bind rfcomm0 "$MAC"; then
    echo "obd_bt_bind: bound /dev/rfcomm0 to $MAC"
else
    echo "obd_bt_bind: rfcomm bind failed for $MAC — is it paired and in range?" >&2
    exit 1
fi
