#!/usr/bin/env python3
"""
obd_gatekeeper.py — OBD2 Port Gatekeeper (runs permanently on the Raspberry Pi)

The Pi is wired between the vehicle's CAN bus and the OBD2 port connector.
Default state: complete silence — the connector looks dead to scan tools,
laptops, and unauthorised dongles.

When Archer OS boots and connects via USB serial:
  1. Pi receives ARCHER_AUTH_REQ
  2. Pi sends a 32-byte random challenge nonce + UTC timestamp
  3. Client responds with HMAC-SHA256(shared_key, nonce + ':' + timestamp)
  4. Pi verifies:
       a. Timestamp is within ±30 seconds (replay protection)
       b. HMAC matches (key proof)
       c. Using constant-time compare (prevents timing attacks)
  5. Correct key → "AUTH_OK" + switch to transparent ELM327 proxy mode
     Wrong key   → silence, port stays locked

Replay protection: The timestamp-in-MAC means a captured challenge+response
pair cannot be reused once the 30-second window closes, even if the nonce
is somehow intercepted.

Key rotation: KEY_FILE may contain two newline-separated hex keys:
  Line 1 — current key (always tried first)
  Line 2 — previous key (optional; tried as fallback during rotation window)
To rotate: prepend new key to file as line 1, keep old as line 2 for one
session, then remove line 2.

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
AUTH_PORT     = os.environ.get("GATEKEEPER_AUTH_PORT", "/dev/ttyAMA0")
ELM_PORT      = os.environ.get("GATEKEEPER_ELM_PORT",  "/dev/ttyAMA1")
BAUD          = 115200
RELAY_PIN     = 17     # BCM — HIGH = OBD2 unlocked, LOW = locked
OVERRIDE_PIN  = 27     # BCM — physical emergency override switch (pull-up, active-low)
OVERRIDE_HOLD = 3.0    # seconds switch must be held to trigger (prevents accidental trips)
AUTH_TIMEOUT  = 10.0   # seconds to complete the full handshake
TIMESTAMP_WINDOW = 30  # seconds — reject challenges older than this
MAX_SESSION_SECS = 4 * 3600  # force re-auth after 4 hours

# Rate limiting — protects against brute-force / fuzzing attacks
MAX_FAILURES_SOFT  = 3   # → 30s lockout
MAX_FAILURES_HARD  = 6   # → 300s lockout
MAX_FAILURES_PERM  = 10  # → indefinite lockout (manual reset required)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [GATEKEEPER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── failure tracking ─────────────────────────────────────────────────

_fail_count   = 0
_locked_until = 0.0


def _record_failure():
    global _fail_count, _locked_until
    _fail_count += 1
    if _fail_count >= MAX_FAILURES_PERM:
        _locked_until = float('inf')
        log.critical(f"PERMANENT LOCKOUT after {_fail_count} failures — manual reset required")
    elif _fail_count >= MAX_FAILURES_HARD:
        _locked_until = time.time() + 300
        log.warning(f"HARD LOCKOUT 300s after {_fail_count} failures")
    elif _fail_count >= MAX_FAILURES_SOFT:
        _locked_until = time.time() + 30
        log.warning(f"SOFT LOCKOUT 30s after {_fail_count} failures")


def _check_lockout() -> bool:
    """Return True if currently locked out."""
    if _locked_until == float('inf'):
        return True
    if time.time() < _locked_until:
        remaining = int(_locked_until - time.time())
        log.warning(f"Locked out — {remaining}s remaining")
        return True
    return False


def _reset_failures():
    global _fail_count, _locked_until
    _fail_count   = 0
    _locked_until = 0.0


# ── relay control ────────────────────────────────────────────────────

def setup_relay():
    if not GPIO_AVAILABLE:
        log.info("RPi.GPIO not available — relay in stub mode")
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_PIN,    GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(OVERRIDE_PIN, GPIO.IN,  pull_up_down=GPIO.PUD_UP)  # active-low
    log.info(f"Relay on GPIO {RELAY_PIN}: OBD2 port LOCKED")
    log.info(f"Emergency override on GPIO {OVERRIDE_PIN} (hold {OVERRIDE_HOLD}s)")


# ── emergency override switch ─────────────────────────────────────────

_override_active = threading.Event()


def _watch_override():
    """Monitor the physical override switch in a background thread.

    The switch must be held for OVERRIDE_HOLD seconds (prevents accidental
    activation by vibration or a momentary short). When triggered it unlocks
    the OBD2 relay and sets _override_active so the main loop skips auth.
    The override is cleared when the switch is released.
    """
    if not GPIO_AVAILABLE:
        return
    log.info("Override switch monitor thread running")
    hold_start = None
    while True:
        pressed = (GPIO.input(OVERRIDE_PIN) == GPIO.LOW)
        if pressed:
            if hold_start is None:
                hold_start = time.time()
            elif time.time() - hold_start >= OVERRIDE_HOLD and not _override_active.is_set():
                log.warning(
                    f"EMERGENCY OVERRIDE activated (GPIO {OVERRIDE_PIN} held "
                    f"{OVERRIDE_HOLD}s) — OBD2 unlocked without auth"
                )
                _override_active.set()
                set_relay(True)
        else:
            if _override_active.is_set():
                log.info("Emergency override released — re-locking OBD2 port")
                _override_active.clear()
                set_relay(False)
            hold_start = None
        time.sleep(0.1)


def set_relay(unlocked: bool):
    state_str = "UNLOCKED" if unlocked else "LOCKED"
    if GPIO_AVAILABLE:
        GPIO.output(RELAY_PIN, GPIO.HIGH if unlocked else GPIO.LOW)
    log.info(f"OBD2 relay: {state_str}")


# ── key loading ──────────────────────────────────────────────────────

def load_keys() -> list[bytes]:
    """Load one or two keys from KEY_FILE.

    File format:
      <current_key_hex>
      <previous_key_hex>   (optional — used during rotation)

    Returns a list with 1-2 key byte strings. Current key is always first.
    """
    with open(KEY_FILE, "r") as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]
    if not lines:
        raise ValueError("Key file is empty")
    return [bytes.fromhex(line) for line in lines[:2]]


# ── proxy thread ─────────────────────────────────────────────────────

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
    """Bridge auth_port ↔ ELM327 port transparently after auth.

    Enforces MAX_SESSION_SECS: closes the session and re-locks the port
    after 4 hours regardless of activity, forcing periodic re-auth.
    """
    try:
        elm = serial.Serial(ELM_PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        log.error(f"Cannot open ELM port {ELM_PORT}: {e}")
        return

    log.info(f"Proxy mode active — session expires in {MAX_SESSION_SECS//3600}h")
    session_start = time.time()
    stop_event    = threading.Event()

    def _expire_session():
        time.sleep(MAX_SESSION_SECS)
        if not stop_event.is_set():
            log.info("Session expired — forcing re-auth")
            try:
                auth_port.write(b"REAUTH_REQUIRED\n")
                auth_port.flush()
            except serial.SerialException:
                pass
            elm.close()

    t_expire = threading.Thread(target=_expire_session, daemon=True)
    t_expire.start()

    t1 = threading.Thread(target=_proxy, args=(auth_port, elm, "archer→elm"),  daemon=True)
    t2 = threading.Thread(target=_proxy, args=(elm, auth_port, "elm→archer"),  daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    stop_event.set()
    elapsed = int(time.time() - session_start)
    log.info(f"Proxy session ended after {elapsed}s — re-locking OBD2 port")


# ── authentication handshake ─────────────────────────────────────────

def handle_connection(port: serial.Serial, keys: list[bytes]) -> bool:
    """Run one challenge-response cycle with timestamp-based replay protection.

    Protocol:
      Client → "ARCHER_AUTH_REQ\\n"
      Pi     → "CHALLENGE:{nonce_hex}:{unix_ts}\\n"
      Client → "RESPONSE:{hmac_hex}\\n"
         where hmac = HMAC-SHA256(key, nonce_bytes + b':' + str(ts).encode())
      Pi     → "AUTH_OK\\n"  (or silence on failure)

    Timestamp binding prevents replay: a captured challenge+response is only
    valid within the TIMESTAMP_WINDOW seconds it was issued.

    Key rotation: tries each key in `keys` list. Current key (index 0) is
    always tried first; previous key (index 1) is a fallback during rotation.

    Returns True only on successful authentication.
    """
    port.timeout = AUTH_TIMEOUT

    try:
        line = port.readline().decode("ascii", errors="replace").strip()
    except serial.SerialException:
        return False

    if line != "ARCHER_AUTH_REQ":
        log.warning(f"Unrecognised opener: {line!r} — ignoring")
        return False

    # Issue challenge: fresh nonce + current UTC timestamp
    nonce = secrets.token_bytes(32)
    ts    = int(time.time())
    try:
        port.write(f"CHALLENGE:{nonce.hex()}:{ts}\n".encode("ascii"))
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
        _record_failure()
        return False

    client_mac = response_line[len("RESPONSE:"):]

    # Verify timestamp is still within window (replay protection)
    now = time.time()
    if abs(now - ts) > TIMESTAMP_WINDOW:
        log.warning(f"Timestamp out of window ({abs(now - ts):.1f}s) — rejecting")
        _record_failure()
        return False

    # The signed payload: nonce bytes + ':' + timestamp string
    signed_payload = nonce + b':' + str(ts).encode()

    # Try each key (current first, previous as rotation fallback)
    for key_idx, key in enumerate(keys):
        expected_mac = hmac.new(key, signed_payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(client_mac.lower(), expected_mac.lower()):
            try:
                port.write(b"AUTH_OK\n")
                port.flush()
            except serial.SerialException:
                return False
            if key_idx > 0:
                log.warning("Authenticated with PREVIOUS key — rotate key file soon")
            else:
                log.info("Authentication PASSED")
            _reset_failures()
            return True

    log.warning("Authentication FAILED — wrong key, staying silent")
    _record_failure()
    return False


# ── main loop ────────────────────────────────────────────────────────

def main():
    setup_relay()

    try:
        keys = load_keys()
    except Exception as e:
        log.critical(f"Cannot load key from {KEY_FILE}: {e}")
        sys.exit(1)

    log.info(f"OBD2 Gatekeeper running on {AUTH_PORT} — {len(keys)} key(s) loaded")
    log.info(f"Port is LOCKED — timestamp window: ±{TIMESTAMP_WINDOW}s, max session: {MAX_SESSION_SECS//3600}h")

    t_override = threading.Thread(target=_watch_override, daemon=True, name="override-switch")
    t_override.start()

    def _shutdown(sig, _frame):
        log.info("Shutdown signal — locking OBD2 port")
        set_relay(False)
        if GPIO_AVAILABLE:
            GPIO.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    while True:
        # Physical override switch bypasses the auth/lockout flow entirely
        if _override_active.is_set():
            time.sleep(1)
            continue

        if _check_lockout():
            time.sleep(5)
            continue
        try:
            with serial.Serial(AUTH_PORT, BAUD, timeout=AUTH_TIMEOUT) as port:
                authenticated = handle_connection(port, keys)
                if authenticated:
                    set_relay(True)
                    run_proxy_session(port)
                    set_relay(False)
                    # Reload keys after each session to pick up rotation changes
                    try:
                        keys = load_keys()
                        log.info(f"Keys reloaded: {len(keys)} key(s)")
                    except Exception as e:
                        log.error(f"Key reload failed: {e} — keeping previous keys")
        except serial.SerialException as e:
            log.error(f"Serial error on {AUTH_PORT}: {e} — retrying in 3s")
            time.sleep(3)
        except Exception as e:
            log.error(f"Unexpected error: {e} — retrying in 3s")
            time.sleep(3)


if __name__ == "__main__":
    main()
