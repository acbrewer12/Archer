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

Relay polarity — REQUIRED before the port will ever unlock: the "complete
silence" default above holds only if driving GPIO 17 low actually opens the CAN
H/L passthrough. On an active-LOW relay board it does the opposite, and the port
would sit open whenever the Pi is off, booting, or crashed. This is not
detectable by reading the pin back (that returns the output latch, not the
contacts), so the service refuses all authentication until polarity is
established either by a wired sense contact (GATEKEEPER_RELAY_SENSE_PIN —
preferred, re-checked on every transition) or by a recorded bench measurement
(`obd_gatekeeper.py --verify-relay`). See the "relay polarity" section below and
docs/HARDWARE_BRINGUP.md §3.1.

Install as a systemd service on the Pi:
  sudo cp obd_gatekeeper.py /opt/archer/obd_gatekeeper.py
  sudo cp key_manager.py /opt/archer/key_manager.py    # needed for scope lookup
  sudo cp obd_gatekeeper.service /etc/systemd/system/
  sudo python3 /opt/archer/obd_gatekeeper.py --verify-relay   # unless sense pin wired
  sudo systemctl enable --now obd_gatekeeper

Physical wiring (Pi ↔ OBD2 connector):
  Pi GPIO 17 (BCM) → Relay IN  (controls CAN H/L passthrough)
  Pi GPIO 22 (BCM) ← Relay sense contact, optional but recommended (dry contact
                     to GND; spare DPDT pole or aux NO — never CAN H/L directly)
  Pi /dev/ttyAMA0  → OBD2 pin 7 (K-Line) or custom auth pins 11/12
  Pi internal UART → ELM327 chip or direct CAN transceiver (SN65HVD230)
"""

import os
import sys
import hmac
import hashlib
import json
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

KEY_FILE      = "/etc/archer/obd_auth.key"
AUTH_PORT     = os.environ.get("GATEKEEPER_AUTH_PORT", "/dev/ttyAMA0")
ELM_PORT      = os.environ.get("GATEKEEPER_ELM_PORT",  "/dev/ttyAMA1")
BAUD          = 115200
RELAY_PIN     = 17     # BCM — polarity is NOT assumed; see below and _relay_level()
# OPEN HARDWARE QUESTION (the highest-consequence one in this file) — the hardware
# is still unmeasured, but the code no longer GUESSES at the answer.
#
# This constant used to be annotated "HIGH = OBD2 unlocked, LOW = locked". That
# mapping is correct only for an ACTIVE-HIGH relay module — one that energises its
# coil when IN is driven high. A large fraction of the cheap opto-isolated relay
# boards sold for Pi use are ACTIVE-LOW and energise when IN is pulled low. Note
# that OVERRIDE_PIN below is explicitly annotated active-low, so polarity was
# clearly on the author's mind for the input; no assumption was ever stated for
# this output, and no board has ever been measured.
#
# If the installed board is active-low, the polarity is inverted end to end and the
# OBD2 port sits UNLOCKED whenever the Pi is off, still booting, or crashed — the
# exact opposite of the "default state: complete silence" guarantee the module
# docstring opens with. It fails OPEN, and it does so silently: the logs would read
# "OBD2 relay: LOCKED" the whole time.
#
# Because that failure mode is both fail-open and invisible, this file no longer
# trusts the mapping. It refuses to unlock the port at all until polarity has been
# established, either by a wired sense contact (RELAY_SENSE_PIN — empirical, and
# re-checked on every transition) or by a recorded bench measurement (POLARITY_FILE,
# attested once by a human with a multimeter). See the "relay polarity" section
# below for the mechanism, for why reading the pin back proves nothing, and for why
# an unverified system stays running-but-locked rather than exiting.
#
# STILL NEEDS REAL HARDWARE: the de-energised (Pi unpowered) contact state. That is
# the state that actually decides fail-safe vs fail-open, no software can observe
# it, and the fix if it is wrong is a pull resistor, not code.
# See docs/HARDWARE_BRINGUP.md §3.1.
RELAY_ACTIVE_HIGH_ASSUMED = True   # historical assumption — used ONLY as the
                                   # fallback drive level while blocked, never as
                                   # grounds to unlock. Verification overrides it.

# BCM pin wired to a DRY CONTACT that follows the relay's switched state, or None
# when no feedback is wired. This is the only way the Pi can actually observe what
# the relay did rather than what it was told to do — GPIO.input() on RELAY_PIN
# itself just reads back the output latch and proves nothing.
#
# WIRING (read this before picking a pin): use the spare pole of a DPDT relay, or
# an auxiliary/NO contact on the relay board, wired as a dry contact between this
# GPIO and GND, with the internal pull-up enabled. NEVER tap CAN H or CAN L into a
# GPIO to sense this — they are not logic-level, they sit around 2.5V idle and
# swing above the Pi's 3.3V tolerance, and loading the differential pair risks
# disrupting the bus on a moving vehicle. The sense contact must be galvanically
# separate from the CAN path.
RELAY_SENSE_PIN = (
    int(os.environ["GATEKEEPER_RELAY_SENSE_PIN"])
    if os.environ.get("GATEKEEPER_RELAY_SENSE_PIN", "").strip().isdigit()
    else None
)
# True when the sense contact is CLOSED (reads LOW, pulled to GND) in the relay's
# UNLOCKED/passthrough state. Flip via env if the spare pole is wired to the NC
# side instead of NO.
RELAY_SENSE_CLOSED_MEANS_UNLOCKED = (
    os.environ.get("GATEKEEPER_RELAY_SENSE_INVERT", "").strip().lower() != "true"
)

# Bench-measured polarity attestation, written by `obd_gatekeeper.py --verify-relay`.
POLARITY_FILE = os.environ.get("GATEKEEPER_POLARITY_FILE", "/etc/archer/relay_polarity.json")

OVERRIDE_PIN  = 27     # BCM — physical emergency override switch (pull-up, active-low)
OVERRIDE_HOLD = 3.0    # seconds switch must be held to trigger (prevents accidental trips)
AUTH_TIMEOUT  = 10.0   # seconds to complete the full handshake
TIMESTAMP_WINDOW = 30  # seconds — reject challenges older than this
# OPEN HARDWARE QUESTION: both of the above are round numbers with no measurement
# behind them — nothing in this project has ever run the handshake over a real UART.
# On the arithmetic, AUTH_TIMEOUT is very unlikely to be too SHORT: the whole
# exchange is two round trips of ~120 bytes at 115200 baud, roughly 10ms of wire
# time, so 10s carries about 1000x headroom. The interesting direction is the other
# one — this same value is how long main()'s idle loop blocks per iteration, so it
# also sets the service's minimum reaction time to a client appearing and bounds how
# long a stalled client can hold AUTH_PORT open.
#
# TIMESTAMP_WINDOW is the sharper unknown, because it is not a latency budget at all
# — it is a clock-AGREEMENT budget between two machines. A Pi with no RTC boots to
# whatever timestamp was last written to disk and cannot land inside ±30s until
# systemd-timesyncd gets a route, which on a truck hotspot may be minutes or never.
# When it fails the operator sees "Timestamp out of window", each attempt calls
# _record_failure(), and ten of those reach permanent lockout (see MAX_FAILURES_PERM
# below) — so the most likely first-boot failure of this whole system is a clock,
# not a key. Widening the window is NOT the fix: it widens the replay window by
# exactly the same amount. Real hardware needs to answer two things — the measured
# wall time of a real handshake, and how far the Pi's clock has actually drifted at
# the moment obd_auth_client.py first runs after a cold boot. If the latter is
# routinely >30s the answer is a battery-backed RTC, not a larger constant.
# See docs/HARDWARE_BRINGUP.md §3.2 and §2.5.
MAX_SESSION_SECS = 4 * 3600  # force re-auth after 4 hours

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

# NOTE for first-hardware bring-up (not a defect — stated because the consequence is
# easy to miss): _fail_count is process-global and DECAYS NEVER. It is zeroed only by
# a successful authentication, via _reset_failures() at the bottom of
# handle_connection(). Ten failures across the entire lifetime of the service —
# spread over weeks, or accumulated in thirty seconds by retrying through a
# clock-skew problem — reach MAX_FAILURES_PERM and lock the port indefinitely. Since
# the state is in memory only, the "manual reset" the critical log line refers to is
# `sudo systemctl restart obd_gatekeeper` (Restart=always, RestartSec=3). The
# physical override switch also still works while locked out: main() tests
# _override_active before _check_lockout(), which is the deliberate escape hatch.
# See docs/HARDWARE_BRINGUP.md §2.5.


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


# ── relay polarity ───────────────────────────────────────────────────
#
# The problem this section exists to solve: if the installed relay board is
# active-LOW rather than the active-HIGH this file historically assumed, every
# lock/unlock decision is inverted and the OBD2 port sits OPEN whenever the Pi is
# unpowered, booting, or crashed. That is a fail-open security model, and it would
# not announce itself — the logs would cheerfully read "OBD2 relay: LOCKED" while
# the connector was live.
#
# WHAT CANNOT BE DONE: there is no software-only self-test for this. On a pin
# configured as an output, GPIO.input(RELAY_PIN) returns the output latch — the
# value just written — not the state of the coil, the contacts, or anything
# downstream. A "self-test" built on reading back the pin would pass 100% of the
# time on both an active-high and an active-low board. That is not a weaker test
# than the real thing, it is a strictly harmful one: it converts an honest unknown
# into a logged green checkmark. It is deliberately not implemented.
#
# WHAT CAN BE DONE, in descending order of strength:
#
#   1. RELAY_SENSE_PIN — a dry contact following the switched state (see the wiring
#      note at the constant). This is genuine observation: drive the pin, read the
#      contact back, and compare. It catches an inverted board, a relay that never
#      energises (dead coil, wrong drive voltage, missing JD-VCC jumper), and a
#      contact welded shut from switching inductive load. Checked on every
#      transition, not just at boot, so a relay that fails closed mid-session is
#      caught too. This is the recommended install and the hardware has not been
#      bought yet, so there is still time to spec a DPDT part.
#
#   2. POLARITY_FILE — a human attestation recorded once at the bench with a
#      multimeter, via `obd_gatekeeper.py --verify-relay`. Weaker: it is a claim
#      about a measurement taken at one moment, not a live reading, and it cannot
#      notice the relay dying later. But it is a real measurement of the real
#      board, which is exactly what was missing.
#
#   3. Neither — the port never unlocks. See _polarity_blocked below.
#
# WHY AN UNVERIFIED SYSTEM STAYS RUNNING RATHER THAN EXITING (the non-obvious part):
# the instinct on "refuse to proceed" is sys.exit(1). That is precisely wrong here.
# The GPIO only holds a defined level while some process is driving it; when this
# service exits, the pin reverts to its power-on default (an input with no pull on
# a Pi), the relay board's IN floats, and on an active-low board that floating input
# is read as the ASSERTED state. Exiting to be safe would therefore unlock the port
# on exactly the hardware the check is meant to protect against. So an unverified
# gatekeeper stays alive, keeps driving the pin at its best-guess locked level, and
# refuses every authentication instead — loudly, on a repeating timer.
#
# WHAT NONE OF THIS FIXES: the window between the vehicle powering the Pi and this
# code executing GPIO.setup() — several seconds of kernel boot during which the pin
# is undriven. That is not solvable in software at any level. It needs a physical
# pull resistor on the relay IN line (pull-down for an active-high board, pull-up
# for active-low) sized against the board's input impedance, so the de-energised
# state is defined before anything boots. Recorded as an open item in
# docs/HARDWARE_BRINGUP.md §3.1 — measure the unpowered state during bring-up.

# Resolved polarity for this process. None until established; set by
# verify_relay_polarity() at startup.
_relay_active_high: Optional[bool] = None
_polarity_source   = "unverified"
# When True, authentication is refused outright — polarity could not be
# established, so "unlock" has no trustworthy meaning.
_polarity_blocked  = True


def _relay_level(unlocked: bool, active_high: bool) -> int:
    """Map a logical relay state to the physical level to drive.

    Pure and total — the one place the polarity mapping lives, so it can be
    tested without a Pi. Returns GPIO.HIGH/GPIO.LOW when RPi.GPIO is present,
    and the equivalent 1/0 otherwise so the mapping stays testable off-hardware.
    """
    high = GPIO.HIGH if GPIO_AVAILABLE else 1
    low  = GPIO.LOW  if GPIO_AVAILABLE else 0
    if active_high:
        return high if unlocked else low
    return low if unlocked else high


def load_relay_polarity() -> Optional[dict]:
    """Read and validate the bench-measured polarity attestation.

    Returns the attestation dict, or None if absent/malformed/incomplete. Being
    strict here is deliberate: a half-written or hand-edited file must not read as
    a verification.
    """
    try:
        with open(POLARITY_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Polarity attestation at {POLARITY_FILE} is unreadable: {e}")
        return None

    if not isinstance(data, dict):
        log.error(f"Polarity attestation at {POLARITY_FILE} is not an object")
        return None
    if not isinstance(data.get("active_high"), bool):
        log.error(f"Polarity attestation at {POLARITY_FILE} has no boolean 'active_high'")
        return None
    # Require provenance. An attestation with nobody's name on it is a guess that
    # someone wrote down, and the whole point of this file is that a human looked
    # at a meter.
    for field in ("verified_by", "verified_at", "method"):
        if not str(data.get(field, "")).strip():
            log.error(f"Polarity attestation at {POLARITY_FILE} is missing '{field}'")
            return None
    return data


def read_relay_sense() -> Optional[bool]:
    """Observe the relay's ACTUAL switched state via the sense contact.

    Returns True if observed UNLOCKED, False if observed LOCKED, or None when no
    sense pin is wired (or GPIO is unavailable) and the state is unobservable.
    """
    if RELAY_SENSE_PIN is None or not GPIO_AVAILABLE:
        return None
    closed = (GPIO.input(RELAY_SENSE_PIN) == GPIO.LOW)  # pulled up; closed → LOW
    return closed if RELAY_SENSE_CLOSED_MEANS_UNLOCKED else (not closed)


def verify_relay_polarity() -> bool:
    """Establish relay polarity empirically if possible, by attestation otherwise.

    Sets the module-level polarity state and returns True when the port may be
    unlocked. Never raises — a failure here must leave the service running and
    locked, not crash it (see the section header for why exiting is unsafe).
    """
    global _relay_active_high, _polarity_source, _polarity_blocked

    if not GPIO_AVAILABLE:
        # No GPIO means no relay is being driven at all — nothing is being
        # protected and nothing can be misrepresented. Test/dev path.
        _relay_active_high = RELAY_ACTIVE_HIGH_ASSUMED
        _polarity_source   = "stub (no GPIO)"
        _polarity_blocked  = False
        log.info("Relay polarity check skipped — stub mode, no GPIO to verify")
        return True

    # ── 1. Empirical: drive each state and read the contact back ──────────
    if RELAY_SENSE_PIN is not None:
        results = {}
        for candidate_active_high in (True, False):
            # Drive the level that WOULD mean "locked" under this candidate
            # polarity, then observe. Exactly one candidate can be consistent.
            GPIO.output(RELAY_PIN, _relay_level(False, candidate_active_high))
            time.sleep(0.05)   # relay mechanical settle (datasheets: ~5-10ms)
            observed_locked = (read_relay_sense() is False)
            GPIO.output(RELAY_PIN, _relay_level(True, candidate_active_high))
            time.sleep(0.05)
            observed_unlocked = (read_relay_sense() is True)
            results[candidate_active_high] = observed_locked and observed_unlocked

        # Always leave the pin at the assumed-locked level before deciding.
        GPIO.output(RELAY_PIN, _relay_level(False, RELAY_ACTIVE_HIGH_ASSUMED))

        consistent = [p for p, ok in results.items() if ok]
        if len(consistent) == 1:
            _relay_active_high = consistent[0]
            _polarity_source   = f"measured via sense pin GPIO {RELAY_SENSE_PIN}"
            _polarity_blocked  = False
            GPIO.output(RELAY_PIN, _relay_level(False, _relay_active_high))
            log.info(
                f"Relay polarity VERIFIED empirically: "
                f"{'ACTIVE-HIGH' if _relay_active_high else 'ACTIVE-LOW'} "
                f"(sense pin GPIO {RELAY_SENSE_PIN})"
            )
            if not _relay_active_high:
                log.warning(
                    "Board is ACTIVE-LOW — the port is UNLOCKED whenever this Pi is "
                    "unpowered or booting. Fit a pull-up on the relay IN line."
                )
            return True

        # Both or neither consistent → the contact is not tracking the drive.
        _polarity_blocked = True
        _relay_active_high = RELAY_ACTIVE_HIGH_ASSUMED
        _polarity_source   = "sense pin INCONSISTENT"
        log.critical(
            f"RELAY SENSE INCONSISTENT — driving GPIO {RELAY_PIN} does not change "
            f"the sense contact on GPIO {RELAY_SENSE_PIN} as either polarity predicts "
            f"(results={results}). Likely causes: contacts welded shut, coil not "
            f"energising (check relay supply / JD-VCC jumper), sense wired to the "
            f"wrong pole, or GATEKEEPER_RELAY_SENSE_INVERT set wrongly. "
            f"REFUSING ALL AUTHENTICATION until this is resolved."
        )
        return False

    # ── 2. Attested: a human measured it at the bench ─────────────────────
    attestation = load_relay_polarity()
    if attestation is not None:
        _relay_active_high = attestation["active_high"]
        _polarity_source   = (
            f"attested by {attestation['verified_by']} on {attestation['verified_at']} "
            f"({attestation['method']})"
        )
        _polarity_blocked  = False
        log.info(
            f"Relay polarity from attestation: "
            f"{'ACTIVE-HIGH' if _relay_active_high else 'ACTIVE-LOW'} — {_polarity_source}"
        )
        log.info(
            f"No sense pin wired — polarity is NOT re-checked at runtime. A relay that "
            f"fails closed later will not be detected. Wiring GATEKEEPER_RELAY_SENSE_PIN "
            f"upgrades this to a live check."
        )
        if not _relay_active_high:
            log.warning(
                "Board is ACTIVE-LOW — the port is UNLOCKED whenever this Pi is "
                "unpowered or booting. Fit a pull-up on the relay IN line."
            )
        return True

    # ── 3. Unverified — run, hold the pin, refuse to unlock ───────────────
    _relay_active_high = RELAY_ACTIVE_HIGH_ASSUMED
    _polarity_source   = "unverified"
    _polarity_blocked  = True
    log.critical(
        "RELAY POLARITY UNVERIFIED — refusing all authentication. This service "
        "cannot tell whether driving GPIO %d high locks or unlocks the OBD2 port, "
        "and guessing wrong means the connector is live whenever the Pi is off. "
        "Resolve with EITHER: (a) wire a sense contact and set "
        "GATEKEEPER_RELAY_SENSE_PIN (preferred — live verification), or (b) measure "
        "the board with a multimeter and record it: sudo %s --verify-relay. "
        "Holding GPIO %d at the assumed-locked level meanwhile.",
        RELAY_PIN, os.path.abspath(__file__), RELAY_PIN,
    )
    return False


# ── relay control ────────────────────────────────────────────────────

def setup_relay():
    if not GPIO_AVAILABLE:
        log.info("RPi.GPIO not available — relay in stub mode")
        verify_relay_polarity()
        return
    GPIO.setmode(GPIO.BCM)
    # Set up at the assumed-locked level first, then let verify_relay_polarity()
    # correct it. Under an active-low board this initial level is wrong (it holds
    # the port unlocked) for the few milliseconds until verification runs — an
    # unavoidable ordering artefact, since the pin must be an output before it can
    # be driven at all, and vastly shorter than the multi-second boot window that
    # precedes it either way. The pull resistor discussed above is what actually
    # covers this.
    GPIO.setup(RELAY_PIN, GPIO.OUT,
               initial=_relay_level(False, RELAY_ACTIVE_HIGH_ASSUMED))
    GPIO.setup(OVERRIDE_PIN, GPIO.IN,  pull_up_down=GPIO.PUD_UP)  # active-low
    if RELAY_SENSE_PIN is not None:
        GPIO.setup(RELAY_SENSE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        log.info(f"Relay sense contact on GPIO {RELAY_SENSE_PIN}")

    verify_relay_polarity()

    log.info(f"Relay on GPIO {RELAY_PIN}: OBD2 port LOCKED ({_polarity_source})")
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
                # The override deliberately bypasses the polarity gate. It is a
                # physical action by someone standing at the vehicle who has
                # decided they need the port open now, and the failure this whole
                # mechanism guards against (silently fail-open) is not made worse
                # by an explicit human unlock request. But it cannot be honestly
                # reported as succeeding when polarity is unknown, so say so.
                if _polarity_blocked:
                    log.critical(
                        "Override is driving GPIO %d on UNVERIFIED polarity (%s) — "
                        "if the board is inverted this LOCKS instead of unlocking.",
                        RELAY_PIN, _polarity_source,
                    )
                    active_high = (_relay_active_high if _relay_active_high is not None
                                   else RELAY_ACTIVE_HIGH_ASSUMED)
                    if GPIO_AVAILABLE:
                        GPIO.output(RELAY_PIN, _relay_level(True, active_high))
                else:
                    set_relay(True)
        else:
            if _override_active.is_set():
                log.info("Emergency override released — re-locking OBD2 port")
                _override_active.clear()
                set_relay(False)
            hold_start = None
        time.sleep(0.1)


def set_relay(unlocked: bool) -> bool:
    """Drive the relay to `unlocked`, confirming it when a sense contact exists.

    Returns True if the relay is believed to be in the requested state. A False
    return on an unlock request means the caller MUST NOT proceed with the
    session — the port is not in a known state.
    """
    state_str = "UNLOCKED" if unlocked else "LOCKED"

    # Refuse to unlock on unverified polarity. Locking is always permitted:
    # under the assumed polarity it is correct, and under the inverted one the
    # port was already open regardless, so driving the pin cannot make it worse.
    if unlocked and _polarity_blocked:
        log.critical(
            "REFUSING to unlock OBD2 relay — polarity unverified (%s). "
            "See --verify-relay.", _polarity_source
        )
        return False

    active_high = (_relay_active_high if _relay_active_high is not None
                   else RELAY_ACTIVE_HIGH_ASSUMED)

    if GPIO_AVAILABLE:
        GPIO.output(RELAY_PIN, _relay_level(unlocked, active_high))

    # Confirm against the sense contact. This is the check that catches a relay
    # failing during service, not just a mis-specified one at boot: a welded
    # contact or a dead coil shows up here as a mismatch on the next transition.
    observed = read_relay_sense()
    if observed is not None:
        time.sleep(0.05)   # mechanical settle before believing the reading
        observed = read_relay_sense()
        if observed != unlocked:
            log.critical(
                "RELAY DID NOT FOLLOW COMMAND — asked for %s, sense contact reads %s. "
                "Treating the OBD2 port as COMPROMISED and refusing the session.",
                state_str, "UNLOCKED" if observed else "LOCKED",
            )
            # Best effort: command locked again. If the relay is stuck this
            # achieves nothing electrically, but it leaves the pin correct for
            # the case where only the unlock drive failed.
            if GPIO_AVAILABLE:
                GPIO.output(RELAY_PIN, _relay_level(False, active_high))
            return False

    log.info(f"OBD2 relay: {state_str}")
    return True


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

    # OPEN HARDWARE QUESTION: this read and the RESPONSE read below both use
    # readline(), which on a serial.Serial returns on '\n' OR on timeout, whichever
    # comes first — and a timeout return is a PARTIAL line that is indistinguishable
    # from a legitimately short one. Every test that covers this function
    # (test_archer.py TestGatekeeperHandshake) mocks the port and hands back whole
    # lines atomically, so partial-read behaviour has never once been exercised.
    #
    # A truncated read HERE is harmless: it logs "Unrecognised opener" and retries
    # without recording a failure. A truncated read at the RESPONSE line below is
    # not — it yields a short client_mac, fails compare_digest, and calls
    # _record_failure(), which means ordinary line noise counts toward the lockout
    # tiers. Ten such events over the lifetime of the process (the counter never
    # decays) is a permanent lockout caused by electrical noise rather than an
    # attacker. Worth contrasting with _proxy() above, which already handles this
    # correctly for the post-auth path: it buffers and splits on '\r', holding an
    # unterminated command over to the next read. The handshake path has no
    # equivalent buffering.
    #
    # Real hardware is the only way to size the risk: run the handshake a few
    # hundred times over the actual /dev/ttyAMA0 wiring WITH THE ENGINE RUNNING
    # (alternator ripple, injector transients, starter draw) and count how many
    # produce "Expected RESPONSE, got:" with a truncated value. If that count is
    # non-zero, the fix is either to give this path the same buffer-until-terminator
    # treatment _proxy() has, or to have _record_failure() distinguish "malformed
    # frame" from "wrong key" so noise cannot drive the lockout tiers.
    # See docs/HARDWARE_BRINGUP.md §3.3.
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

    _last_polarity_nag = 0.0

    while True:
        # Physical override switch bypasses the auth/lockout flow entirely
        if _override_active.is_set():
            time.sleep(1)
            continue

        # Polarity gate. Deliberately a loop guard rather than a startup exit: the
        # process must stay alive to keep driving RELAY_PIN at a defined level (see
        # the relay polarity section for why exiting fails OPEN on an active-low
        # board). Re-announced on a timer so the condition cannot scroll off the
        # top of a log and be quietly forgotten.
        if _polarity_blocked:
            if time.time() - _last_polarity_nag > 60:
                log.critical(
                    "OBD2 port held LOCKED and ALL authentication refused — relay "
                    "polarity unverified (%s). Run: sudo %s --verify-relay",
                    _polarity_source, os.path.abspath(__file__),
                )
                _last_polarity_nag = time.time()
            time.sleep(5)
            continue

        if _check_lockout():
            time.sleep(5)
            continue
        try:
            with serial.Serial(AUTH_PORT, BAUD, timeout=AUTH_TIMEOUT) as port:
                session_permissions = handle_connection(port, keys)
                if session_permissions is not None:
                    # EVALUATED, not just flagged — there is no race between the
                    # relay's physical switching speed and AUTH_TIMEOUT, contrary to
                    # what the ordering might suggest at a glance. AUTH_TIMEOUT
                    # bounds the readline() calls inside handle_connection() only,
                    # and that call has already returned by the time this line runs;
                    # nothing re-arms it. A mechanical relay settles in ~5-10ms,
                    # orders of magnitude below any timeout in this file.
                    #
                    # What IS unconfirmed is narrower and needs real hardware: the
                    # client is told AUTH_OK inside handle_connection(), BEFORE the
                    # contacts here have moved, so a client that fires its first OBD
                    # command the instant it reads AUTH_OK can put bytes on the wire
                    # during the settle window. That would cost a single dropped
                    # first command (an ELM327 would simply not answer, and the
                    # client's own retry covers it) — it is not a security boundary
                    # problem, since the relay is still the only thing gating the
                    # bus. Watch for a missing/ignored first command on first
                    # bring-up; if it shows up, the fix is to move the AUTH_OK write
                    # after set_relay(True), which is a protocol-ordering change and
                    # deliberately not being made on untested assumptions.
                    # See docs/HARDWARE_BRINGUP.md §3.10.
                    #
                    # set_relay() now returns False when the unlock could not be
                    # confirmed (sense contact disagrees, or polarity unverified).
                    # A session must not start in that case: the port is not in a
                    # known state, and proxying bus traffic through it would be
                    # acting on exactly the assumption this check exists to stop
                    # trusting.
                    if not set_relay(True):
                        log.error("Unlock not confirmed — refusing session, re-locking")
                        set_relay(False)
                        continue
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


def _verify_relay_wizard() -> int:
    """Interactive bench procedure that records a measured polarity attestation.

    This exists because the Pi cannot see the relay. It walks an operator through
    driving each level and reading the contacts with a multimeter, then records
    what they measured. The value is entirely in the human doing the measurement —
    so the prompts insist on the relay being DISCONNECTED from the vehicle, and
    the file records who measured it and how.
    """
    from datetime import datetime, timezone

    print("=" * 70)
    print("Archer OBD Gatekeeper — relay polarity verification")
    print("=" * 70)
    print()
    print("This records which GPIO level actually CLOSES the CAN H/L passthrough,")
    print("so the gatekeeper stops assuming. Until it is recorded (or a sense")
    print("contact is wired), the gatekeeper refuses all authentication.")
    print()
    print("SAFETY — do this at the bench, not on the truck:")
    print("  * The relay must be DISCONNECTED from the vehicle CAN H/L lines.")
    print("  * You need a multimeter on continuity across the switched contacts.")
    print("  * If a sense contact is wired instead, you do not need this at all —")
    print("    set GATEKEEPER_RELAY_SENSE_PIN and the check runs automatically.")
    print()

    if not GPIO_AVAILABLE:
        print("ERROR: RPi.GPIO unavailable — run this on the Pi itself.")
        return 1

    if input("Is the relay disconnected from the vehicle? [yes/NO] ").strip().lower() != "yes":
        print("Aborted — disconnect the relay from the CAN lines first.")
        return 1

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)

    try:
        print()
        print(f"Driving GPIO {RELAY_PIN} LOW. Probe the switched contacts now.")
        GPIO.output(RELAY_PIN, GPIO.LOW)
        time.sleep(0.5)
        low_closed = input("  Continuity across the contacts at LOW? [yes/no] ").strip().lower()

        print()
        print(f"Driving GPIO {RELAY_PIN} HIGH. Probe again.")
        GPIO.output(RELAY_PIN, GPIO.HIGH)
        time.sleep(0.5)
        high_closed = input("  Continuity across the contacts at HIGH? [yes/no] ").strip().lower()

        GPIO.output(RELAY_PIN, GPIO.LOW)

        low_c  = low_closed.startswith("y")
        high_c = high_closed.startswith("y")

        if low_c == high_c:
            print()
            print("INCONSISTENT: the contacts read the same at both levels.")
            print("The relay is not switching. Check the coil supply, the JD-VCC")
            print("jumper, and that IN is on the pin you think it is. Nothing recorded.")
            return 1

        # Continuity == passthrough connected == port UNLOCKED.
        active_high = high_c
        print()
        print(f"  → Board is ACTIVE-{'HIGH' if active_high else 'LOW'}.")
        if not active_high:
            print()
            print("  WARNING: active-low. The port is UNLOCKED whenever this Pi is")
            print("  unpowered, booting, or crashed. Fit a pull-UP on the relay IN")
            print("  line so the de-energised state is defined before boot, and")
            print("  re-measure with the Pi powered off to confirm it holds.")

        print()
        print("Also measure the state that actually matters for fail-safe:")
        unpowered = input("  With the Pi UNPOWERED, are the contacts open (port locked)? [yes/no/skipped] ").strip().lower()

        who = input("Your name (recorded in the attestation): ").strip()
        if not who:
            print("A name is required — the attestation is a record of who measured it.")
            return 1

        payload = {
            "active_high":      active_high,
            "verified_by":      who,
            "verified_at":      datetime.now(timezone.utc).isoformat(),
            "method":           "bench multimeter continuity across switched contacts",
            "unpowered_state":  unpowered,
            "relay_pin":        RELAY_PIN,
        }
        os.makedirs(os.path.dirname(POLARITY_FILE), exist_ok=True)
        with open(POLARITY_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        os.chmod(POLARITY_FILE, 0o600)

        print()
        print(f"Recorded to {POLARITY_FILE}:")
        print(json.dumps(payload, indent=2))
        print()
        print("Restart the gatekeeper to pick it up:")
        print("  sudo systemctl restart obd_gatekeeper")
        if unpowered.startswith("n"):
            print()
            print("NOTE: you reported the port is NOT locked with the Pi unpowered.")
            print("That is a fail-open install. Fix it with a pull resistor before")
            print("this goes in the truck — the software cannot cover that window.")
        return 0

    except (KeyboardInterrupt, EOFError):
        print("\nAborted — nothing recorded.")
        return 1
    finally:
        try:
            GPIO.output(RELAY_PIN, GPIO.LOW)
        except Exception:
            pass


if __name__ == "__main__":
    if "--verify-relay" in sys.argv[1:]:
        sys.exit(_verify_relay_wizard())
    main()
