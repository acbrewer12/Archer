#!/usr/bin/env python3
"""
config_sanity_check.py — Server-to-Pi configuration sanity handshake.

Called as the FIRST statement in obd_gatekeeper.py's main(), before that
file's own HMAC challenge-response starts (before setup_relay() and before
load_keys()) — see obd_gatekeeper.py's own _run_config_sanity_check(). This
is independent of, and prior to, that handshake: it exists to catch
operator-config DRIFT between what the server and the Pi each
independently believe about four values, not to authenticate a client.
The real OBD2 port security perimeter remains obd_gatekeeper.py's own HMAC
handshake, unchanged by this file.

Motivating case (Self Hosting vault note, "Real, flagged maintainability
risk"): the server's own Tailscale IP is hardcoded across several of its
own config files (Grafana, Caddy). If that IP ever changes (Tailscale
reassignment, server rebuild) and the Pi's own copy of "what IP the
server is at" isn't updated to match, the Pi previously had no way to
notice — it would just keep running, silently pointed at a stale server
address. This check makes that failure loud instead: on any mismatch,
obd_gatekeeper.py exits before its relay is ever set unlocked.

Two of the four checked values are computed FRESH from the Pi's own real
files/constants (the key fingerprint from the actual /etc/archer/obd_auth.key
on disk; the tier-permission set from this module's own copy of
key_manager.py's three generate_*_key() permission lists) — there is no
separate "Pi's declared expectation" for these two to drift from a live
value, since the live value IS what gets compared. The other two (server
Tailscale IP, OBDLink serial) have no live-hardware or live-file source to
read on the Pi — no OBDLink serial-read command exists anywhere in this
codebase today, confirmed by inspecting archer.py's obd_autodetect() ELM327
AT command set — so those two are each operator-set expectations on BOTH
sides. This check only proves the two independently-configured copies
agree with each other; it does NOT verify the OBDLink's serial against
real, live hardware, and that gap needs real-hardware validation once a
physical OBDLink MX+ exists to query.

Server-side counterpart: blueprints/terminal.py's POST /terminal/pi_config_check
(reuses the same ARCHER_PI_TOKEN shared secret and a Gatekeeper-style
timestamp window as terminal.py's other pi_* routes — not a second,
separate credential).

Install alongside obd_gatekeeper.py and key_manager.py (same directory —
see obd_gatekeeper.py's own module docstring for the full install steps):
  sudo cp config_sanity_check.py /opt/archer/config_sanity_check.py
"""
import hashlib
import json
import time
import urllib.error
import urllib.request

KEY_FILE = "/etc/archer/obd_auth.key"

# Must be kept in sync with key_manager.py's generate_owner_key() /
# generate_mechanic_key() / generate_readonly_key() ("permissions" metadata)
# and obd_gatekeeper.py's own OWNER_PERMISSIONS fallback, and with
# blueprints/terminal.py's matching _EXPECTED_TIER_PERMISSIONS on the
# server. This is a third, explicit copy of that same information, not a
# fix for the existing duplication — the whole point of this check is to
# catch copies disagreeing, so an extra named copy with an alarm attached
# is the intended shape here, not an oversight. Consolidating all of these
# into one real source of truth is a separate, larger follow-up.
EXPECTED_TIER_PERMISSIONS = {
    "OWNER":    ["read_all", "write_all", "admin"],
    "MECHANIC": ["read_all", "write_non_security"],
    "READONLY": ["read_all"],
}


def _local_key_fingerprint() -> str:
    """SHA-256 hex digest of the real key file's raw bytes — matches
    exactly what an operator gets from `sha256sum /etc/archer/obd_auth.key`
    when setting GATEKEEPER_KEY_FINGERPRINT in the server's archer.env."""
    with open(KEY_FILE, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _fetch_server_config(server_url: str, pi_token: str, timeout: float = 10.0) -> dict:
    """POST to the server's /terminal/pi_config_check with a fresh
    timestamp — the same pre-shared-secret + timestamp-window idea as
    obd_gatekeeper.py's own handshake (TIMESTAMP_WINDOW), sized for a
    single-request config check rather than re-implementing that
    handshake's full nonce exchange for something that isn't the actual
    security perimeter."""
    body = json.dumps({"token": pi_token, "timestamp": int(time.time())}).encode()
    req = urllib.request.Request(
        f"{server_url.rstrip('/')}/terminal/pi_config_check",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def check_config(server_url: str, pi_token: str,
                  expected_server_ip: str, expected_obdlink_serial: str,
                  timeout: float = 10.0):
    """Fetch the server's ground-truth config and compare against this
    Pi's own values.

    Returns (ok: bool, problems: list[str]) — problems is empty iff ok is
    True. Never raises for a normal mismatch; a genuine network/auth
    failure surfaces as a single problem string and is itself treated as
    a refusal to start — fail-closed on doubt, never fail-open."""
    try:
        server_cfg = _fetch_server_config(server_url, pi_token, timeout)
    except Exception as e:
        return False, [f"could not reach server config-check endpoint: {e}"]

    problems = []

    server_ip = server_cfg.get("server_tailscale_ip")
    if server_ip != expected_server_ip:
        problems.append(
            f"server Tailscale IP mismatch: server reports {server_ip!r}, "
            f"Pi expected {expected_server_ip!r}"
        )

    server_fp = server_cfg.get("gatekeeper_key_fingerprint")
    try:
        local_fp = _local_key_fingerprint()
    except OSError as e:
        problems.append(f"could not read local key file to compute fingerprint: {e}")
        local_fp = None
    if local_fp is not None and server_fp != local_fp:
        problems.append(
            f"Gatekeeper HMAC key fingerprint mismatch: server expects "
            f"{server_fp!r}, Pi's actual key file hashes to {local_fp!r}"
        )

    server_serial = server_cfg.get("obdlink_serial")
    if server_serial != expected_obdlink_serial:
        problems.append(
            f"OBDLink serial mismatch: server expects {server_serial!r}, "
            f"Pi expected {expected_obdlink_serial!r}"
        )

    server_perms = server_cfg.get("tier_permissions")
    if server_perms != EXPECTED_TIER_PERMISSIONS:
        problems.append(
            f"tier-permission set mismatch: server has {server_perms!r}, "
            f"Pi has {EXPECTED_TIER_PERMISSIONS!r}"
        )

    return (len(problems) == 0), problems
