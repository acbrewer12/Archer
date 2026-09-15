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

Scoped access (post-auth command filtering): once a key authenticates, its
scope is looked up via key_manager.py (KEYS_DIR metadata: OWNER / MECHANIC /
READONLY, with "permissions" and optional "expires"). Every subsequent OBD
command is checked against that scope before being forwarded to the real
bus — a READONLY key cannot send anything but read requests, a MECHANIC key
additionally gets DTC-clear but not actuator/security commands, and a key
that key_manager.py has marked expired is rejected outright even though the
HMAC still matches. A key that HMAC-authenticates but has no key_manager.py
record at all (e.g. one deployed via archer-os/obd-auth/keygen.sh, which
predates key_manager.py) is treated as full OWNER access for backward
compatibility. See the "command scope enforcement" section below for exactly
what is and isn't filtered.

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
  sudo cp key_manager.py /opt/archer/key_manager.py    # needed for scope lookup
  sudo cp config_sanity_check.py /opt/archer/config_sanity_check.py  # server config check, runs first
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
from typing import List, Optional

# RPi.GPIO is optional — runs in test/stub mode without it
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# key_manager.py supplies scoped-key metadata (permissions/expiry) used to
# filter commands after auth. Must be deployed alongside this file (see
# "Install as a systemd service" above) — degrades to owner-equivalent
# access with a loud warning if it's missing, rather than failing to start.
try:
    import key_manager
    KEY_MANAGER_AVAILABLE = True
except ImportError:
    KEY_MANAGER_AVAILABLE = False

# config_sanity_check.py is a required sibling file (same directory), not
# an optional pip dependency — see its own module docstring for what it
# does and why it runs before anything else in main(). A bare import only
# resolves when this file's own directory is on sys.path, which is true
# when run directly on the Pi (script dir is sys.path[0]) but NOT when
# imported as the pi.obd_gatekeeper package submodule (e.g. test_archer.py's
# TestGatekeeperHandshake) — the explicit insert makes the same bare
# import work in both contexts without relying on the caller's sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_sanity_check

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

# ── server config sanity check (config_sanity_check.py) ──────────────
# ARCHER_URL/ARCHER_PI_TOKEN reuse the exact same values pi_connect.sh's
# own env vars already use for the same "how do I reach/authenticate to
# the server" purpose — not a second, redundant pair of secrets/URLs for
# the same question.
ARCHER_URL                 = os.environ.get("ARCHER_URL", "")
ARCHER_PI_TOKEN             = os.environ.get("ARCHER_PI_TOKEN", "")
PI_EXPECTED_SERVER_IP       = os.environ.get("PI_EXPECTED_SERVER_IP", "")
PI_EXPECTED_OBDLINK_SERIAL  = os.environ.get("PI_EXPECTED_OBDLINK_SERIAL", "")

# Rate limiting — protects against brute-force / fuzzing attacks
MAX_FAILURES_SOFT  = 3   # → 30s lockout
MAX_FAILURES_HARD  = 6   # → 300s lockout
MAX_FAILURES_PERM  = 10  # → indefinite lockout (manual reset required)

# Permission set granted to keys that authenticate but have no key_manager.py
# record (legacy/unregistered keys — see module docstring) — mirrors the
# "permissions" list key_manager.generate_owner_key() writes to owner.json.
OWNER_PERMISSIONS = ["read_all", "write_all", "admin"]

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


# ── command scope enforcement ───────────────────────────────────────
#
# Once authenticated, the client is normally free to send any OBD-II /
# ELM327 command straight to the vehicle bus. That's correct for an OWNER
# key, but a MECHANIC or READONLY key (key_manager.py) is supposed to be
# limited to a subset of commands — this table + classify_command() is
# what actually enforces that limit, using key_manager.py's existing
# "permissions" list (read_all / write_non_security / write_all / admin)
# rather than inventing a new scheme.
#
# Classification is by OBD-II/UDS *service (mode) ID* — the first hex byte
# of a request (e.g. "010C" → mode "01"). This is the same granularity
# key_manager.py's metadata already uses (it has no per-PID data), so this
# is the most faithful mapping possible without changing key_manager.py.
#
# WHAT THIS DOES NOT COVER (see final report for the full list):
#   - No per-PID filtering within a mode (e.g. within Mode 01, a READONLY
#     key can request ANY PID, since key_manager.py has no PID-level data).
#   - AT* adapter-configuration commands (ATZ, ATSP0, ATH1, ...) are always
#     allowed for any authenticated key — they configure the ELM327/UART
#     adapter itself, not the vehicle bus, and key_manager.py has no
#     concept of restricting them.
#   - Any UDS/manufacturer-specific mode not explicitly recognised below
#     is treated as security-sensitive (default-deny, not default-allow).

READ_MODES = {"01", "02", "03", "05", "06", "07", "09", "0A"}

# Mode 04 (Clear Diagnostic Trouble Codes) is a write, but not a
# security-sensitive one — it only clears fault codes/readiness monitors;
# it cannot move an actuator, unlock anything, or reflash the ECU.
NON_SECURITY_WRITE_MODES = {"04"}

# Everything else — Mode 08 (bidirectional actuator control), 0B, 10
# (diagnostic session control), 11 (ECU reset), 27 (security access /
# seed-key unlock), 28 (communication control), 2E (write data by
# identifier), 2F (I/O control by identifier — direct output override),
# 31 (routine control), 34-37 (firmware up/download), 3D, 85, and any
# mode not listed above — requires "write_all" (OWNER/ADMIN only).
SECURITY_WRITE_MODES = {
    "08", "0B", "10", "11", "27", "28", "2E", "2F", "31",
    "34", "35", "36", "37", "3D", "85",
}


def classify_command(cmd: str) -> str:
    """Classify one OBD/ELM327 command line into a required permission.

    Returns one of "AT" (adapter config — always allowed), "read_all",
    "write_non_security", or "write_all" (covers both explicitly-known
    security-sensitive modes and anything unrecognised — default-deny).
    """
    body = cmd.strip().upper()
    if not body:
        return "AT"  # blank line / keepalive — harmless
    if body.startswith("AT"):
        return "AT"
    mode = body[:2]
    if mode in READ_MODES:
        return "read_all"
    if mode in NON_SECURITY_WRITE_MODES:
        return "write_non_security"
    return "write_all"  # SECURITY_WRITE_MODES + unknown/unrecognised


def command_allowed(cmd: str, permissions: List[str]) -> bool:
    """Return True if `permissions` (from key_manager.py) permits `cmd`."""
    required = classify_command(cmd)
    if required == "AT":
        return True
    if required == "read_all":
        return "read_all" in permissions
    if required == "write_non_security":
        return "write_non_security" in permissions or "write_all" in permissions
    return "write_all" in permissions


def _resolve_permissions(key: bytes) -> Optional[List[str]]:
    """Resolve the permission list to enforce for a session authenticated
    with `key`.

    Returns None if the key is a registered key_manager.py key that has
    EXPIRED — callers must treat that as an authentication failure, since
    the HMAC matching alone doesn't know about expiry.
    """
    if not KEY_MANAGER_AVAILABLE:
        log.warning(
            "key_manager module unavailable — cannot resolve key scope, "
            "granting full OWNER access (deploy key_manager.py alongside "
            "obd_gatekeeper.py to enable scoped enforcement)"
        )
        return OWNER_PERMISSIONS

    info = key_manager.verify_key_type(key)
    if not info.get("found"):
        # Not registered via key_manager.py (e.g. archer-os/obd-auth/keygen.sh
        # flow) — historically this flat key has always meant "owner".
        return OWNER_PERMISSIONS
    if info.get("expired"):
        return None
    return info.get("permissions", [])


# ── proxy thread ─────────────────────────────────────────────────────

def _proxy(src: serial.Serial, dst: serial.Serial, label: str,
           permissions: Optional[List[str]] = None):
    """Copy bytes from src → dst until either port closes.

    When `permissions` is given, the byte stream is treated as CR-terminated
    OBD/ELM327 command lines and each one is checked with command_allowed()
    before being forwarded — anything outside the authenticated key's scope
    is dropped and logged instead of reaching the real OBD bus. When
    `permissions` is None the direction is relayed unfiltered (used for the
    ELM→client response path, which carries bus responses, not commands).
    """
    if permissions is None:
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
        return

    buf = b""
    try:
        while True:
            data = src.read(256)
            if not data:
                break
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                cmd = line.decode("ascii", errors="replace")
                if command_allowed(cmd, permissions):
                    dst.write(line + b"\r")
                    dst.flush()
                else:
                    log.warning(
                        f"BLOCKED command outside key scope: {cmd!r} "
                        f"(permissions={permissions})"
                    )
            # Any bytes left in `buf` are a not-yet-terminated command and
            # are held over to the next read rather than forwarded early.
    except serial.SerialException:
        pass
    log.info(f"Proxy thread {label} exited")


def run_proxy_session(auth_port: serial.Serial, permissions: List[str]):
    """Bridge auth_port ↔ ELM327 port after auth, filtering commands sent
    by the client (auth_port → elm) against `permissions`.

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

    t1 = threading.Thread(target=_proxy, args=(auth_port, elm, "archer→elm", permissions), daemon=True)
    t2 = threading.Thread(target=_proxy, args=(elm, auth_port, "elm→archer", None),        daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    stop_event.set()
    elapsed = int(time.time() - session_start)
    log.info(f"Proxy session ended after {elapsed}s — re-locking OBD2 port")


# ── authentication handshake ─────────────────────────────────────────

def handle_connection(port: serial.Serial, keys: list[bytes]) -> Optional[List[str]]:
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

    Returns the permission list to enforce for the session (resolved via
    key_manager.py — see _resolve_permissions) on success, or None on
    failure — including when the HMAC matches but key_manager.py reports
    the key as expired.
    """
    port.timeout = AUTH_TIMEOUT

    try:
        line = port.readline().decode("ascii", errors="replace").strip()
    except serial.SerialException:
        return None

    if line != "ARCHER_AUTH_REQ":
        log.warning(f"Unrecognised opener: {line!r} — ignoring")
        return None

    # Issue challenge: fresh nonce + current UTC timestamp
    nonce = secrets.token_bytes(32)
    ts    = int(time.time())
    try:
        port.write(f"CHALLENGE:{nonce.hex()}:{ts}\n".encode("ascii"))
        port.flush()
    except serial.SerialException:
        return None

    log.info("Challenge issued")

    try:
        response_line = port.readline().decode("ascii", errors="replace").strip()
    except serial.SerialException:
        return None

    if not response_line.startswith("RESPONSE:"):
        log.warning(f"Expected RESPONSE, got: {response_line!r}")
        _record_failure()
        return None

    client_mac = response_line[len("RESPONSE:"):]

    # Verify timestamp is still within window (replay protection)
    now = time.time()
    if abs(now - ts) > TIMESTAMP_WINDOW:
        log.warning(f"Timestamp out of window ({abs(now - ts):.1f}s) — rejecting")
        _record_failure()
        return None

    # The signed payload: nonce bytes + ':' + timestamp string
    signed_payload = nonce + b':' + str(ts).encode()

    # Try each key (current first, previous as rotation fallback)
    for key_idx, key in enumerate(keys):
        expected_mac = hmac.new(key, signed_payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(client_mac.lower(), expected_mac.lower()):
            # Resolve scope BEFORE sending AUTH_OK so an expired key_manager.py
            # key can still be rejected even though the HMAC matched.
            permissions = _resolve_permissions(key)
            if permissions is None:
                log.warning("Authentication REJECTED — key is registered but EXPIRED (key_manager.py)")
                _record_failure()
                return None

            try:
                port.write(b"AUTH_OK\n")
                port.flush()
            except serial.SerialException:
                return None
            if key_idx > 0:
                log.warning("Authenticated with PREVIOUS key — rotate key file soon")
            else:
                log.info("Authentication PASSED")
            log.info(f"Session permissions: {permissions}")
            _reset_failures()
            return permissions

    log.warning("Authentication FAILED — wrong key, staying silent")
    _record_failure()
    return None


# ── main loop ────────────────────────────────────────────────────────

def _run_config_sanity_check():
    """Fail closed if this Pi's config disagrees with the server's, or if
    the server can't be reached to check at all — never fail open on
    doubt. See config_sanity_check.py's own module docstring for the full
    design rationale.

    SKIP_CONFIG_SANITY_CHECK=1 bypasses this entirely — bench
    testing/development only, never set on the real, in-vehicle Pi."""
    if os.environ.get("SKIP_CONFIG_SANITY_CHECK", "") == "1":
        log.warning("SKIP_CONFIG_SANITY_CHECK=1 — server config sanity check bypassed")
        return
    if not ARCHER_URL or not ARCHER_PI_TOKEN:
        log.critical(
            "ARCHER_URL and ARCHER_PI_TOKEN must both be set to run the server "
            "config sanity check (or set SKIP_CONFIG_SANITY_CHECK=1 for bench "
            "testing only) — refusing to start"
        )
        sys.exit(1)

    ok, problems = config_sanity_check.check_config(
        ARCHER_URL, ARCHER_PI_TOKEN,
        PI_EXPECTED_SERVER_IP, PI_EXPECTED_OBDLINK_SERIAL,
    )
    if not ok:
        log.critical("SERVER CONFIG SANITY CHECK FAILED — refusing to start:")
        for p in problems:
            log.critical(f"  - {p}")
        sys.exit(1)
    log.info("Server config sanity check passed")


def main():
    _run_config_sanity_check()

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
                session_permissions = handle_connection(port, keys)
                if session_permissions is not None:
                    set_relay(True)
                    run_proxy_session(port, session_permissions)
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
