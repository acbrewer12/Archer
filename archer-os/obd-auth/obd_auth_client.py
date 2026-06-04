#!/usr/bin/env python3
"""
obd_auth_client.py — OBD2 Port Authentication Client (runs on Archer OS at boot)

Sends an HMAC-SHA256 challenge-response to the Pi gatekeeper.
If auth succeeds, the Pi switches to transparent ELM327 proxy mode and
OBD2 data flows normally through /dev/ttyUSB0 for the rest of the session.

Exit 0 = authenticated, OBD2 port unlocked.
Exit 1 = failed or no Pi present — Archer runs without OBD data.
"""

import sys
import os
import hmac
import hashlib
import serial
import time

KEY_FILE  = "/etc/archer/obd_auth.key"
OBD_PORT  = os.environ.get("OBD_AUTH_PORT", "/dev/ttyUSB0")
BAUD      = 115200
TIMEOUT   = 5.0   # seconds to wait for each Pi response


def load_key() -> bytes:
    try:
        with open(KEY_FILE, "r") as f:
            raw = f.read().strip()
        return bytes.fromhex(raw)
    except FileNotFoundError:
        print(f"[OBD_AUTH] No key at {KEY_FILE} — OBD auth disabled", file=sys.stderr)
        sys.exit(0)   # Not an error — just skip auth if no key deployed
    except Exception as e:
        print(f"[OBD_AUTH] Key load error: {e}", file=sys.stderr)
        sys.exit(1)


def authenticate() -> int:
    key = load_key()

    try:
        port = serial.Serial(OBD_PORT, BAUD, timeout=TIMEOUT)
    except serial.SerialException as e:
        print(f"[OBD_AUTH] Cannot open {OBD_PORT}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Step 1: Announce ourselves
        port.write(b"ARCHER_AUTH_REQ\n")
        port.flush()

        # Step 2: Receive challenge nonce
        line = port.readline().decode("ascii", errors="replace").strip()
        if not line.startswith("CHALLENGE:"):
            print(f"[OBD_AUTH] Unexpected response: {line!r}", file=sys.stderr)
            return 1
        nonce_hex = line[len("CHALLENGE:"):]
        if len(nonce_hex) != 64:
            print(f"[OBD_AUTH] Bad nonce length ({len(nonce_hex)})", file=sys.stderr)
            return 1
        nonce = bytes.fromhex(nonce_hex)

        # Step 3: Compute HMAC-SHA256(shared_key, nonce)
        mac = hmac.new(key, nonce, hashlib.sha256).hexdigest()

        # Step 4: Send response
        port.write(f"RESPONSE:{mac}\n".encode("ascii"))
        port.flush()

        # Step 5: Wait for verdict
        result = port.readline().decode("ascii", errors="replace").strip()
        if result == "AUTH_OK":
            print("[OBD_AUTH] Authenticated — OBD2 port unlocked", file=sys.stderr)
            return 0
        else:
            print(f"[OBD_AUTH] Rejected by Pi: {result!r}", file=sys.stderr)
            return 1

    except serial.SerialTimeoutException:
        print("[OBD_AUTH] Timeout — Pi did not respond", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[OBD_AUTH] Error during handshake: {e}", file=sys.stderr)
        return 1
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(authenticate())
