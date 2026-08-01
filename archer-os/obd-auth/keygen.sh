#!/bin/bash
# keygen.sh — Generate the shared HMAC-SHA256 key for OBD2 port authentication.
#
# Run this ONCE on a trusted machine. Then:
#   1. The key is embedded into the Archer USB image at build time.
#   2. Copy the key to the Pi: scp obd_auth.key pi@<pi-ip>:/etc/archer/obd_auth.key
#
# DO NOT regenerate unless you also re-flash the Pi.
# DO NOT commit obd_auth.key to git (it is in .gitignore).

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KEY_FILE="$SCRIPT_DIR/obd_auth.key"

if [ -f "$KEY_FILE" ]; then
    echo ""
    echo "  WARNING: Key already exists at:"
    echo "  $KEY_FILE"
    echo ""
    echo "  Regenerating will lock out the Pi until you re-deploy the new key."
    printf "  Continue? [y/N] "
    read -r confirm
    [ "$confirm" = "y" ] || { echo "Aborted."; exit 0; }
fi

umask 077
openssl rand -hex 32 > "$KEY_FILE"
chmod 600 "$KEY_FILE"

echo ""
echo "  Key generated: $KEY_FILE (mode 600)"
echo "  Fingerprint (sha256, NOT the key itself): $(sha256sum "$KEY_FILE" | cut -d' ' -f1)"
echo ""
echo "  Next steps:"
echo "  1. Deploy to Pi (while on same network as the Pi):"
echo "       scp $KEY_FILE pi@archer-pi:/etc/archer/obd_auth.key"
echo ""
echo "  2. The key is embedded automatically on next 'sudo bash build-vm.sh'"
echo ""
echo "  3. Keep this key file secure — physical access to it = OBD2 access."
echo ""
