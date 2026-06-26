#!/usr/bin/env python3
"""
boot_auth.py — Archer OS USB boot authentication.

Runs at Archer OS boot BEFORE anything else.
Connects to the Raspberry Pi gatekeeper via OBD2 serial port.
Performs HMAC-SHA256 challenge-response handshake.
Exits 0 on success (OBD2 port unlocked), exits 1 on failure.

Called by archer-os systemd unit before archer_client.py starts.
"""

import glob
import hmac
import hashlib
import logging
import os
import serial
import serial.tools.list_ports
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEY_FILE    = Path(os.environ.get("OBD_KEY_FILE", "/etc/archer/obd_auth.key"))
BAUD        = 115200
TIMEOUT     = 5.0    # seconds per read
MAX_RETRIES = 3
RETRY_DELAY = 2.0    # seconds between retries

LOG_DIR  = Path("/var/log/archer")
LOG_FILE = LOG_DIR / "boot_auth.log"

# USB VID:PID for common Pi USB serial adapters (CH340, CP210x, FTDI, ACM)
_PI_VENDOR_IDS  = {0x2341, 0x1A86, 0x10C4, 0x0403, 0x0525}  # Arduino/CH340/SiLabs/FTDI/CDC
_PREFER_PATTERN = "/dev/ttyACM*"   # Pi Zero / Pi 4 in CDC gadget mode


# ---------------------------------------------------------------------------
# Logging — goes to both stderr and the persistent log file
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("boot_auth")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("[%(asctime)s] [BOOT_AUTH] %(levelname)s %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")

    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------

def find_gatekeeper_port() -> str | None:
    """
    Scan serial ports for the Raspberry Pi gatekeeper.

    Search order:
      1. /dev/ttyACM* devices (Pi CDC gadget mode — most reliable)
      2. USB serial ports whose VID matches known Pi/adapter vendors
      3. Any remaining /dev/ttyUSB* ports as a last resort

    Returns the device path string, or None if nothing found.
    """
    candidates: list[str] = []

    # Pass 1: ACM devices (Pi USB gadget serial)
    acm_ports = sorted(glob.glob("/dev/ttyACM*"))
    candidates.extend(acm_ports)

    # Pass 2: pyserial enumeration — filter by known VIDs
    for port_info in serial.tools.list_ports.comports():
        if port_info.device in candidates:
            continue
        if port_info.vid in _PI_VENDOR_IDS:
            candidates.append(port_info.device)
            log.debug(f"Found VID-matched port: {port_info.device} "
                      f"(VID={port_info.vid:#06x} PID={port_info.pid:#06x})")

    # Pass 3: remaining USB serial as fallback
    for port_info in serial.tools.list_ports.comports():
        if port_info.device not in candidates and port_info.device.startswith("/dev/ttyUSB"):
            candidates.append(port_info.device)

    if not candidates:
        log.warning("No serial ports found — Pi gatekeeper not detected")
        return None

    log.info(f"Serial port candidates: {candidates}")
    return candidates[0]


# ---------------------------------------------------------------------------
# Authentication handshake
# ---------------------------------------------------------------------------

def _load_key(key_file: Path) -> bytes:
    """Load the shared secret from key_file (hex string on disk)."""
    try:
        raw = key_file.read_text().strip()
        return bytes.fromhex(raw)
    except FileNotFoundError:
        log.error(f"Key file not found: {key_file}")
        raise
    except ValueError as e:
        log.error(f"Key file contains invalid hex data: {e}")
        raise


def authenticate(port: str, key_file: Path) -> bool:
    """
    Perform the HMAC-SHA256 challenge-response handshake with the Pi gatekeeper.

    Protocol (matches obd_gatekeeper.py):
      Client → "ARCHER_AUTH_REQ\\n"
      Pi     → "CHALLENGE:<64-hex-char nonce>\\n"
      Client → "RESPONSE:<64-hex-char HMAC-SHA256>\\n"
      Pi     → "AUTH_OK\\n"  or silence / error string

    Returns True on successful authentication, False otherwise.
    """
    try:
        key = _load_key(key_file)
    except Exception:
        return False

    log.info(f"Opening {port} at {BAUD} baud")
    try:
        conn = serial.Serial(port, BAUD, timeout=TIMEOUT)
    except serial.SerialException as e:
        log.error(f"Cannot open {port}: {e}")
        return False

    try:
        # Step 1: announce ourselves
        conn.write(b"ARCHER_AUTH_REQ\n")
        conn.flush()
        log.debug("Sent ARCHER_AUTH_REQ")

        # Step 2: receive challenge nonce
        line = conn.readline().decode("ascii", errors="replace").strip()
        if not line.startswith("CHALLENGE:"):
            log.error(f"Expected CHALLENGE:..., got: {line!r}")
            return False

        nonce_hex = line[len("CHALLENGE:"):]
        if len(nonce_hex) != 64:
            log.error(f"Nonce length error: got {len(nonce_hex)} hex chars, expected 64")
            return False

        nonce = bytes.fromhex(nonce_hex)
        log.debug("Received 32-byte nonce from Pi")

        # Step 3: compute HMAC-SHA256(key, nonce)
        mac = hmac.new(key, nonce, hashlib.sha256).hexdigest()

        # Step 4: send response
        conn.write(f"RESPONSE:{mac}\n".encode("ascii"))
        conn.flush()
        log.debug("Sent HMAC response")

        # Step 5: receive verdict
        verdict = conn.readline().decode("ascii", errors="replace").strip()
        if verdict == "AUTH_OK":
            log.info("Authentication PASSED — OBD2 port unlocked")
            return True
        else:
            log.error(f"Authentication FAILED — Pi replied: {verdict!r}")
            return False

    except serial.SerialTimeoutException:
        log.error("Timeout waiting for Pi response")
        return False
    except Exception as e:
        log.error(f"Handshake error: {e}")
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("Archer OS boot authentication starting")

    # Allow key file to be overridden via environment for testing
    key_file = Path(os.environ.get("OBD_KEY_FILE", str(KEY_FILE)))

    if not key_file.exists():
        log.warning(f"No key file at {key_file} — skipping OBD auth (running without data)")
        # Not an error — the system can run without OBD2 data access
        return 0

    for attempt in range(1, MAX_RETRIES + 1):
        log.info(f"Attempt {attempt}/{MAX_RETRIES}")

        port = find_gatekeeper_port()
        if port is None:
            log.warning("Gatekeeper not found on any serial port")
            if attempt < MAX_RETRIES:
                log.info(f"Retrying in {RETRY_DELAY}s ...")
                time.sleep(RETRY_DELAY)
            continue

        if authenticate(port, key_file):
            log.info("Boot authentication complete — proceeding to Archer OS")
            print("[BOOT_AUTH] OK — OBD2 unlocked")
            return 0

        log.warning(f"Authentication attempt {attempt} failed")
        if attempt < MAX_RETRIES:
            log.info(f"Retrying in {RETRY_DELAY}s ...")
            time.sleep(RETRY_DELAY)

    log.critical("All authentication attempts failed — halting boot")
    print("[BOOT_AUTH] FAILED — OBD2 port locked. Check key and Pi connection.", file=sys.stderr)
    print(f"[BOOT_AUTH] Log: {LOG_FILE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
