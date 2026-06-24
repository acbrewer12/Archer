#!/usr/bin/env python3
"""
obd_gatekeeper.py — OBD2 Port Gatekeeper (runs permanently on the Raspberry Pi)

The Pi is wired between the vehicle's CAN bus and the OBD2 port connector.
Default state: complete silence — the connector looks dead to scan tools,
laptops, and unauthorised dongles.

When Archer OS boots and connects via USB serial:
  1. Pi receives ARCHER_AUTH_REQ
  2. Pi sends a 32-byte random challenge nonce
  3. Client responds with HMAC-SHA256(shared_key, nonce)
  4. Pi verifies with constant-time compare (prevents timing attacks)
  5. Correct key → "AUTH_OK" + switch to transparent ELM327 proxy mode
     Wrong key   → silence, port stays locked

Install as a systemd service on the Pi:
  sudo cp obd_gatekeeper.py /opt/archer/obd_gatekeeper.py
  sudo cp obd_gatekeeper.service /etc/systemd/system/
  sudo systemctl enable --now obd_gatekeeper

Physical wiring (Pi ↔ OBD2 connector):
  Pi GPIO 17 (BCM) → Relay IN  (controls CAN H/L passthrough)
  Pi /dev/ttyAMA0  → OBD2 pin 7 (K-Line) or custom auth pins 11/12
  Pi internal UART → ELM327 chip or direct CAN transceiver (SN65HVD230)
"""

import os
import sys
import hmac
import hashlib
import serial
import secrets
import time
import logging
import signal
import threading

# RPi.GPIO is optional — runs in test/stub mode without it
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

KEY_FILE      = "/etc/archer/obd_auth.key"
AUTH_PORT     = os.environ.get("GATEKEEPER_AUTH_PORT", "/dev/ttyAMA0")   # Pi UART ↔ OBD2 connector
ELM_PORT      = os.environ.get("GATEKEEPER_ELM_PORT",  "/dev/ttyAMA1")   # Pi UART ↔ ELM327/CAN
BAUD          = 115200
RELAY_PIN     = 17     # BCM — HIGH = OBD2 unlocked, LOW = locked
AUTH_TIMEOUT  = 10.0   # seconds to receive full handshake

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [GATEKEEPER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── relay control ────────────────────────────────────────────────────

def setup_relay():
    if not GPIO_AVAILABLE:
        log.info("RPi.GPIO not available — relay in stub mode")
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)
    log.info(f"Relay on GPIO {RELAY_PIN}: OBD2 port LOCKED")


def set_relay(unlocked: bool):
    state_str = "UNLOCKED" if unlocked else "LOCKED"
    if GPIO_AVAILABLE:
        GPIO.output(RELAY_PIN, GPIO.HIGH if unlocked else GPIO.LOW)
    log.info(f"OBD2 relay: {state_str}")


# ── key loading ──────────────────────────────────────────────────────

def load_key() -> bytes:
    with open(KEY_FILE, "r") as f:
        return bytes.fromhex(f.read().strip())


# ── proxy thread: forwards bytes between auth_port and elm_port ──────

def _proxy(src: serial.Serial, dst: serial.Serial, label: str):
    """Copy bytes from src → dst until either port closes."""
    try:
        while True:
            data = src.read(256)
            if not data:
                break
            dst.write(data)
            dst.flush()
    except serial.SerialException:
        pass
    log.info(f"Proxy thread {label} exited")


def run_proxy_session(auth_port: serial.Serial):
    """
    After authentication, bridge auth_port ↔ ELM327 port transparently.
    Archer's existing pyserial/ELM327 code sees a normal ELM327 device.
    Returns when either side disconnects.
    """
    try:
        elm = serial.Serial(ELM_PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        log.error(f"Cannot open ELM port {ELM_PORT}: {e}")
        return

    log.info("Proxy mode active — forwarding ELM327 data")
    t1 = threading.Thread(target=_proxy, args=(auth_port, elm, "archer→elm"),  daemon=True)
    t2 = threading.Thread(target=_proxy, args=(elm, auth_port, "elm→archer"),  daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    elm.close()
    log.info("Proxy session ended — re-locking OBD2 port")


# ── authentication handshake ─────────────────────────────────────────

def handle_connection(port: serial.Serial, key: bytes) -> bool:
    """
    Run one challenge-response cycle.
    Returns True if the client proved they hold the correct key.
    """
    port.timeout = AUTH_TIMEOUT

    try:
        line = port.readline().decode("ascii", errors="replace").strip()
    except serial.SerialException:
        return False

    if line != "ARCHER_AUTH_REQ":
        # Not our protocol — could be a scan tool. Stay silent.
        log.warning(f"Unrecognised opener: {line!r} — ignoring")
        return False

    # Issue a fresh 32-byte nonce for every attempt (prevents replay attacks)
    nonce = secrets.token_bytes(32)
    try:
        port.write(f"CHALLENGE:{nonce.hex()}\n".encode("ascii"))
        port.flush()
    except serial.SerialException:
        return False

    log.info("Challenge issued")

    try:
        response_line = port.readline().decode("ascii", errors="replace").strip()
    except serial.SerialException:
        return False

    if not response_line.startswith("RESPONSE:"):
        log.warning(f"Expected RESPONSE, got: {response_line!r}")
        return False

    client_mac = response_line[len("RESPONSE:"):]
    expected_mac = hmac.new(key, nonce, hashlib.sha256).hexdigest()

    # Constant-time compare — prevents timing side-channel attacks
    if hmac.compare_digest(client_mac.lower(), expected_mac.lower()):
        try:
            port.write(b"AUTH_OK\n")
            port.flush()
        except serial.SerialException:
            return False
        log.info("Authentication PASSED")
        return True
    else:
        log.warning("Authentication FAILED — wrong key, staying silent")
        # No response sent — the port looks dead to the attacker
        return False


# ── main loop ────────────────────────────────────────────────────────

def main():
    setup_relay()

    try:
        key = load_key()
    except Exception as e:
        log.critical(f"Cannot load key from {KEY_FILE}: {e}")
        sys.exit(1)

    log.info(f"OBD2 Gatekeeper running on {AUTH_PORT} — port is LOCKED")

    def _shutdown(sig, _frame):
        log.info("Shutdown signal — locking OBD2 port")
        set_relay(False)
        if GPIO_AVAILABLE:
            GPIO.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    while True:
        try:
            with serial.Serial(AUTH_PORT, BAUD, timeout=AUTH_TIMEOUT) as port:
                authenticated = handle_connection(port, key)
                if authenticated:
                    set_relay(True)
                    run_proxy_session(port)
                    set_relay(False)
                # Loop back — wait for the next connection attempt
        except serial.SerialException as e:
            log.error(f"Serial error on {AUTH_PORT}: {e} — retrying in 3s")
            time.sleep(3)
        except Exception as e:
            log.error(f"Unexpected error: {e} — retrying in 3s")
            time.sleep(3)


if __name__ == "__main__":
    main()
